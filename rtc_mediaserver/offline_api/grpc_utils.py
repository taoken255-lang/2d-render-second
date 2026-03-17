import asyncio
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import grpc
from grpc import aio

from rtc_mediaserver.config import settings
from rtc_mediaserver.logging_config import get_logger, setup_default_logging
from rtc_mediaserver.proto import render_service_pb2, render_service_pb2_grpc
from rtc_mediaserver.webrtc_server.tools import cleanup_old_results

setup_default_logging()
logger = get_logger(__name__)


def create_aio_channel():
    if settings.grpc_secure_channel:
        creds = grpc.ssl_channel_credentials()
        channel = aio.secure_channel(settings.grpc_server_url, credentials=creds, options=[
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),
            ("grpc.max_send_message_length", 10 * 1024 * 1024),
        ])
    else:
        channel = aio.insecure_channel(settings.grpc_server_url, options=[
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),
            ("grpc.max_send_message_length", 10 * 1024 * 1024),
        ])

    return channel


def info():
    channel = create_aio_channel()
    stub = render_service_pb2_grpc.RenderServiceStub(channel)
    info_response = stub.InfoRouter(render_service_pb2.InfoRequest())
    avatars = info_response.animations
    for avatar in avatars:
        logger.info(f"{str(avatar)}: {info_response.animations[avatar].items}")
        logger.info(f"{str(avatar)}: {info_response.emotions[avatar].items}")


async def video_request(audio: bytes, sample_rate: int, bps: int, avatar_id: str):
    yield render_service_pb2.RenderRequest(
        set_avatar=render_service_pb2.SetAvatar(avatar_id=avatar_id),
        online=True,
        alpha=False,
        output_format="RGB",
    )
    logger.info("AVATAR SENT")

    logger.info((sample_rate, bps))
    chunk_size = int(sample_rate * bps / 8)
    for i in range(0, len(audio), chunk_size):
        chunk = audio[i:i + chunk_size]
        yield render_service_pb2.RenderRequest(
            audio=render_service_pb2.AudioChunk(data=chunk, sample_rate=sample_rate, bps=bps, is_voice=True)
        )
        logger.info("AUD SENT")
        await asyncio.sleep(0)


async def aenumerate(aiterable, start=0):
    idx = start
    async for item in aiterable:
        yield idx, item
        idx += 1

def _drain_stderr(pipe, log_prefix: str):
    try:
        for line in iter(pipe.readline, b""):
            if not line:
                break
            logger.warning("[%s] %s", log_prefix, line.decode(errors="replace").rstrip())
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _ffmpeg_logging_flags() -> list[str]:
    flags = ["-hide_banner", "-loglevel", "verbose" if settings.ffmpeg_debug_enabled else "error"]
    if settings.ffmpeg_debug_enabled:
        flags.extend(["-report", "-stats", "-debug_ts"])
    return flags


def _start_ffmpeg_stderr_thread(proc: subprocess.Popen, log_prefix: str) -> threading.Thread | None:
    if not settings.ffmpeg_debug_enabled or proc.stderr is None:
        return None
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(proc.stderr, log_prefix),
        daemon=True,
        name=f"{log_prefix}-stderr",
    )
    stderr_thread.start()
    return stderr_thread


def _ffmpeg_stderr_target():
    return subprocess.PIPE if settings.ffmpeg_debug_enabled else subprocess.DEVNULL


@contextmanager
def ffmpeg_video_pipe(output_path: str, width: int, height: int, fps: int, vcodec: str = "libx264"):
    frame_bytes = width * height * 3
    cmd = [
        "ffmpeg",
        *_ffmpeg_logging_flags(),
        "-y",
        "-thread_queue_size", "1024",
        "-analyzeduration", "0",
        "-probesize", "32",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "pipe:0",
        "-c:v", vcodec,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-an",
        output_path,
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stderr=_ffmpeg_stderr_target(),
        bufsize=frame_bytes,
    )
    stderr_thread = _start_ffmpeg_stderr_thread(proc, "ffmpeg-video")

    try:
        yield proc, frame_bytes
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        proc.wait()
        if stderr_thread is not None:
            stderr_thread.join(timeout=1)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed (code={proc.returncode})")


def _fit_audio_to_frame_count(audio: bytes, sample_rate: int, bps: int, frame_count: int, fps: int, audio_ch: int = 1) -> bytes:
    bytes_per_sample = max(1, bps // 8)
    sample_count = round(frame_count * sample_rate / fps)
    target_size = sample_count * bytes_per_sample * audio_ch
    logger.info(
        "Sync audio to video frame_count=%s fps=%s sample_rate=%s bps=%s current_audio_bytes=%s target_audio_bytes=%s",
        frame_count,
        fps,
        sample_rate,
        bps,
        len(audio),
        target_size,
    )
    if len(audio) == target_size:
        logger.info("Audio length already matches target duration")
        return audio
    if len(audio) < target_size:
        logger.info("Audio is shorter than video by %s bytes, padding with silence", target_size - len(audio))
        return audio.ljust(target_size, b"\x00")
    logger.info("Audio is longer than video by %s bytes, trimming tail", len(audio) - target_size)
    return audio[:target_size]


def _run_ffmpeg_with_audio_input(cmd: list[str], audio: bytes, log_prefix: str):
    logger.info("Running ffmpeg command: %s", cmd)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stderr=_ffmpeg_stderr_target(),
    )
    stderr_thread = _start_ffmpeg_stderr_thread(proc, log_prefix)

    try:
        if proc.stdin is None:
            raise RuntimeError(f"{log_prefix} stdin is not available")
        proc.stdin.write(audio)
        proc.stdin.close()
        proc.stdin = None
        proc.wait()
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
        if stderr_thread is not None:
            stderr_thread.join(timeout=1)

    if proc.returncode != 0:
        raise RuntimeError(f"{log_prefix} failed (code={proc.returncode})")


def _mux_main_video_audio(
        video_path: Path,
        output_path: Path,
        audio: bytes,
        sample_rate: int,
        audio_fmt: str,
        audio_ch: int = 1):
    started_at = time.time()
    logger.info(
        "Starting main AV mux video_path=%s output_path=%s sample_rate=%s audio_fmt=%s audio_bytes=%s",
        video_path,
        output_path,
        sample_rate,
        audio_fmt,
        len(audio),
    )
    cmd = [
        "ffmpeg",
        *_ffmpeg_logging_flags(),
        "-y",
        "-i", str(video_path),
        "-f", audio_fmt,
        "-ar", str(sample_rate),
        "-ac", str(audio_ch),
        "-i", "pipe:0",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        str(output_path),
    ]
    _run_ffmpeg_with_audio_input(cmd, audio, "ffmpeg-main-mux")
    logger.info("Main AV mux finished successfully: %s, took %.3f sec", output_path, time.time() - started_at)


def _concat_mp4_copy(main_av_path: Path, tail_video_path: Path, output_path: Path, concat_list_path: Path):
    started_at = time.time()
    logger.info(
        "Starting fast concat main_av_path=%s tail_video_path=%s output_path=%s concat_list_path=%s",
        main_av_path,
        tail_video_path,
        output_path,
        concat_list_path,
    )
    concat_list_path.write_text(
        f"file '{main_av_path.resolve().as_posix()}'\nfile '{tail_video_path.resolve().as_posix()}'\n",
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg",
        *_ffmpeg_logging_flags(),
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list_path),
        "-c", "copy",
        str(output_path),
    ]
    logger.info("Running ffmpeg concat command: %s", cmd)
    proc = subprocess.Popen(
        cmd,
        stderr=_ffmpeg_stderr_target(),
    )
    stderr_thread = _start_ffmpeg_stderr_thread(proc, "ffmpeg-concat")
    proc.wait()
    if stderr_thread is not None:
        stderr_thread.join(timeout=1)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed (code={proc.returncode})")
    logger.info("Fast concat finished successfully: %s, took %.3f sec", output_path, time.time() - started_at)

async def local_video_run(
        audio: bytes,
        sample_rate: int,
        bps: int,
        avatar_id: str,
        output_path: Path,
        audio_fmt: str,
        tail_video_path: Path | None = None):
    total_started_at = time.time()

    if not output_path.exists():
        output_path.mkdir(exist_ok=True, parents=True)
    logger.info(
        "local_video_run output_path=%s sample_rate=%s bps=%s avatar_id=%s audio_fmt=%s tail_video_path=%s audio_bytes=%s",
        output_path,
        sample_rate,
        bps,
        avatar_id,
        audio_fmt,
        tail_video_path,
        len(audio),
    )

    channel = create_aio_channel()
    stub = render_service_pb2_grpc.RenderServiceStub(channel)
    response_stream = stub.RenderStream(video_request(
        audio=audio,
        sample_rate=sample_rate,
        bps=bps,
        avatar_id=avatar_id,
    ))

    temp_video_path = output_path / "video_main.mp4"
    main_av_path = output_path / "video_main_av.mp4"
    concat_list_path = output_path / "video_concat.txt"
    video_path = output_path / "video.mp4"
    fps = 25

    frame_count = 0
    frame_width = 0
    frame_height = 0
    pipe_ctx = None
    proc = None
    frame_size = 0

    grpc_started_at = time.time()
    try:
        async for idxa, response_chunk in aenumerate(response_stream):
            logger.info("CHUNK %s", idxa)
            if response_chunk.WhichOneof("chunk") != "video":
                logger.info("Skipping non-video chunk idx=%s kind=%s", idxa, response_chunk.WhichOneof("chunk"))
                continue

            frame = response_chunk.video.data
            logger.info("RECV IMAGE %s %sx%s", frame_count, response_chunk.video.width, response_chunk.video.height)

            if pipe_ctx is None:
                frame_width = response_chunk.video.width
                frame_height = response_chunk.video.height
                logger.info("Start writing to pipe")
                pipe_ctx = ffmpeg_video_pipe(str(temp_video_path), frame_width, frame_height, fps)
                proc, frame_size = pipe_ctx.__enter__()
                logger.info("frame_size expected=%d", frame_size)

            if len(frame) != frame_size:
                logger.error("BAD FRAME SIZE idx=%d got=%d expected=%d", frame_count, len(frame), frame_size)
                raise RuntimeError("frame size mismatch: not raw rgb24 frame")

            proc.stdin.write(frame)

            if proc.poll() is not None:
                logger.error("ffmpeg died with return code %s", proc.returncode)
                raise RuntimeError("ffmpeg died")

            frame_count += 1

        grpc_time = time.time() - grpc_started_at

        if pipe_ctx is None:
            raise RuntimeError("no video frames received from render stream")

        if proc and proc.stdin:
            proc.stdin.close()
            proc.stdin = None
    finally:
        if pipe_ctx is not None:
            pipe_ctx.__exit__(None, None, None)

    synced_audio = _fit_audio_to_frame_count(audio, sample_rate, bps, frame_count, fps)
    final_stage_started_at = time.time()

    await asyncio.to_thread(
        _mux_main_video_audio,
        temp_video_path,
        main_av_path,
        synced_audio,
        sample_rate,
        audio_fmt,
    )


    if tail_video_path is not None:

        await asyncio.to_thread(
            _concat_mp4_copy,
            main_av_path,
            tail_video_path,
            video_path,
            concat_list_path,
        )

    else:
        logger.info("Tail video is not set, moving main AV to final output: %s", video_path)
        main_av_path.replace(video_path)

    concat_time = time.time() - final_stage_started_at

    try:
        temp_video_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to delete temp video: %s", temp_video_path)
    try:
        main_av_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to delete temp AV video: %s", main_av_path)
    try:
        concat_list_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to delete concat list file: %s", concat_list_path)

    await cleanup_old_results()
    logger.info("Old results cleared")
    logger.info("Total offline render took %.3f sec, grpc took %.3f sec, final stage took %.3f sec",
                time.time() - total_started_at,
                grpc_time,
                concat_time
                )
