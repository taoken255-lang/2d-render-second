import asyncio
import shutil
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

import grpc
import wave
from grpc import aio

from rtc_mediaserver.config import settings
from rtc_mediaserver.logging_config import get_logger, setup_default_logging
from rtc_mediaserver.proto import render_service_pb2_grpc, render_service_pb2
from rtc_mediaserver.webrtc_server.constants import AUDIO_SETTINGS
from rtc_mediaserver.webrtc_server.tools import cleanup_old_results

setup_default_logging()
logger = get_logger(__name__)


def create_aio_channel():
    if settings.grpc_secure_channel:
        creds = grpc.ssl_channel_credentials()
        channel = aio.secure_channel(settings.grpc_server_url, credentials=creds, options=[
            ('grpc.max_receive_message_length', 10 * 1024 * 1024),
            ('grpc.max_send_message_length', 10 * 1024 * 1024),
        ])
    else:
        channel = aio.insecure_channel(settings.grpc_server_url, options=[
            ('grpc.max_receive_message_length', 10 * 1024 * 1024),
            ('grpc.max_send_message_length', 10 * 1024 * 1024),
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
            set_avatar=render_service_pb2.SetAvatar(avatar_id=avatar_id), online=True, alpha=False, output_format="RGB"
    )
    logger.info("AVATAR SENT")

    logger.info((sample_rate, 16))
    cur_idx = 0
    chunk_size = int(sample_rate * bps / 8)
    for i in range(0, len(audio), chunk_size):
        chunk = audio[i: i + chunk_size]
        print(len(chunk))
        yield render_service_pb2.RenderRequest(
            audio=render_service_pb2.AudioChunk(data=chunk, sample_rate=sample_rate, bps=bps, is_voice=True)
        )
        logger.info("AUD SENT")
        cur_idx += 1
        await asyncio.sleep(0)


async def aenumerate(aiterable, start=0):
    idx = start
    async for item in aiterable:
        yield idx, item
        idx += 1


def _combine_with_ffmpeg_sync(frames_dir: Path, audio_path: Path, output_path: Path, fps: int):
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate", str(fps),
        "-i", f"{frames_dir}/%d.png",
        "-i", str(audio_path),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        str(output_path)
    ]
    subprocess.run(cmd, check=True)

async def combine_with_ffmpeg(frames_dir: Path, audio_path: Path, output_path: Path, fps: int):
    await asyncio.to_thread(_combine_with_ffmpeg_sync, frames_dir, audio_path, output_path, fps)


async def save_audio_with_ffmpeg(audio: bytes, sample_rate: int, bps: int, output_path: Path):
    """
    Сохраняет байты PCM-аудио в WAV через ffmpeg (асинхронно).
    """
    # Определяем формат по bps
    fmt_map = {8: "u8", 16: "s16le", 24: "s24le", 32: "s32le"}
    fmt = fmt_map.get(bps)
    if not fmt:
        raise ValueError(f"Unsupported bps value: {bps}")

    cmd = [
        "ffmpeg",
        "-y",  # overwrite output
        "-f", fmt,  # raw PCM format
        "-ar", str(sample_rate),  # sample rate
        "-ac", "1",  # mono (если знаешь, что у тебя 1 канал)
        "-i", "pipe:0",  # вход — stdin
        str(output_path)
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate(input=audio)

    if process.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed ({process.returncode})\n{stderr.decode()}"
        )

    print(f"✅ Audio saved: {output_path}")

import os
from contextlib import contextmanager

@contextmanager
def ffmpeg_av_pipe(output_path: str, width: int, height: int, fps: int,
                   vcodec: str = "libx264",
                   audio_sr: int = 16000, audio_ch: int = 1, audio_fmt: str = "s16le"):
    """ffmpeg pipe for video and audio."""

    frame_bytes = width * height * 3  # rgb24

    r_audio, w_audio = os.pipe()
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "verbose",
        "-y",
        "-report",
        "-stats",
        "-debug_ts",

        # VIDEO from stdin
        "-thread_queue_size", "1024",
        "-analyzeduration", "0",
        "-probesize", "32",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "pipe:0",

        # AUDIO from fd=3
        "-thread_queue_size", "1024",
        "-analyzeduration", "0",
        "-probesize", "32",
        "-f", audio_fmt,             # s16le => int16 PCM little-endian
        "-ar", str(audio_sr),
        "-ac", str(audio_ch),
        "-i", f"pipe:{r_audio}",

        # encode
        "-c:v", vcodec,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "aac",

        # stop when one stream ends (обычно удобно)
        # "-shortest",
        output_path,
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        pass_fds=(r_audio,),   # пробрасываем fd в ffmpeg
        stderr=subprocess.DEVNULL,
        bufsize=frame_bytes,
        close_fds=False,
    )

    os.close(r_audio)  # в родителе read-end больше не нужен

    audio_pipe = os.fdopen(w_audio, "wb", buffering=0)

    # def _drain_stderr(p):
    #     for line in p.stderr:
    #         logger.warning("[ffmpeg] %s", line.rstrip())
    #
    # t = threading.Thread(target=_drain_stderr, args=(proc,), daemon=True)
    # t.start()

    try:
        yield proc, frame_bytes, audio_pipe
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            audio_pipe.close()
        except Exception:
            pass
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed (code={proc.returncode})")

def _write_all(stream, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if written is None or written <= 0:
            raise RuntimeError(f"audio write failed: {written}")
        view = view[written:]

async def local_video_run(audio: bytes, sample_rate: int, bps: int, avatar_id: str, output_path: Path, audio_fmt: str):
    if not output_path.exists():
        output_path.mkdir(exist_ok=True, parents=True)
    logger.info(f"Output path: {output_path}")

    channel = create_aio_channel()
    stub = render_service_pb2_grpc.RenderServiceStub(channel)
    response_stream = stub.RenderStream(video_request(
        audio=audio,
        sample_rate=sample_rate,
        bps=bps,
        avatar_id=avatar_id
    ))

    video_path = output_path / "video.mp4"

    img_idx = 0
    pipe_ctx = None
    proc = None
    aout = None
    frame_size = 0
    audio_errors = []
    audio_thread = None

    try:
        async for idxa, response_chunk in aenumerate(response_stream):
            logger.info(f"CHUNK {idxa}")
            if response_chunk.WhichOneof("chunk") != "video":
                continue

            frame = response_chunk.video.data
            logger.info(f"RECV IMAGE {img_idx} {response_chunk.video.width}x{response_chunk.video.height}")

            if pipe_ctx is None:
                logger.info("Start writing to pipe")
                pipe_ctx = ffmpeg_av_pipe(
                    str(video_path),
                    response_chunk.video.width,
                    response_chunk.video.height,
                    25,
                    audio_fmt=audio_fmt,
                    audio_sr=sample_rate,
                )
                proc, frame_size, aout = pipe_ctx.__enter__()

                def _audio_writer():
                    try:
                        _write_all(aout, audio)
                    except Exception as exc:  # noqa: BLE001
                        audio_errors.append(exc)
                    finally:
                        try:
                            aout.close()
                        except Exception:
                            pass

                audio_thread = threading.Thread(target=_audio_writer, daemon=True, name="ffmpeg-audio-writer")
                audio_thread.start()
                logger.info("frame_size expected=%d", frame_size)

            logger.info(f"Write frame {img_idx}")
            if len(frame) != frame_size:
                logger.error("BAD FRAME SIZE idx=%d got=%d expected=%d", img_idx, len(frame), frame_size)
                raise RuntimeError("frame size mismatch: not raw rgb24 frame")

            proc.stdin.write(frame)

            if proc.poll() is not None:
                logger.error(f"ffmpeg died with return code {proc.returncode}")
                raise RuntimeError("ffmpeg died")

            img_idx += 1

        if pipe_ctx is None:
            raise RuntimeError("no video frames received from render stream")

        if proc and proc.stdin:
            proc.stdin.close()
            proc.stdin = None

        if audio_thread:
            audio_thread.join()
        if audio_errors:
            raise RuntimeError(f"audio writer failed: {audio_errors[0]}")
    finally:
        if pipe_ctx is not None:
            pipe_ctx.__exit__(None, None, None)

    await cleanup_old_results()
    logger.info(f"Old results cleared")

