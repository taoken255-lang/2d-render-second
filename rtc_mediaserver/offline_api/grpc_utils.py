import asyncio
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import grpc
import wave
from grpc import aio
from PIL import Image

from rtc_mediaserver.config import settings
from rtc_mediaserver.logging_config import get_logger, setup_default_logging
from rtc_mediaserver.proto import render_service_pb2_grpc, render_service_pb2
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


async def local_video_run(audio: bytes, sample_rate: int, bps: int, avatar_id: str, output_path: Path):
    if not output_path.exists():
        output_path.mkdir(exist_ok=True, parents=True)
    logger.info(f"Output path: {output_path}")
    frames_path = output_path / "frames"
    frames_path.mkdir(parents=True, exist_ok=True)

    channel = create_aio_channel()
    stub = render_service_pb2_grpc.RenderServiceStub(channel)
    response_stream = stub.RenderStream(video_request(
        audio=audio,
        sample_rate=sample_rate,
        bps=bps,
        avatar_id=avatar_id
    ))
    img_idx = 0
    async for idxa, response_chunk in aenumerate(response_stream):
        logger.info(f"CHUNK {idxa}")
        if response_chunk.WhichOneof("chunk") == "video":
            logger.info(f"SAVE IMAGE {img_idx}")
            logger.info(f"{response_chunk.video.width}x{response_chunk.video.height}")
            img = Image.frombytes("RGB", (response_chunk.video.width, response_chunk.video.height),
                response_chunk.video.data, "raw")
            img.save(frames_path / f"{img_idx}.png")
            img_idx += 1
        elif response_chunk.WhichOneof("chunk") == "start_animation":
            logger.info(f"START ANIMATION {response_chunk.start_animation.animation_name}")
        elif response_chunk.WhichOneof("chunk") == "end_animation":
            logger.info(f"END ANIMATION {response_chunk.end_animation.animation_name}")
        elif response_chunk.WhichOneof("chunk") == "emotion_set":
            logger.info(f"EMOTION SET {response_chunk.emotion_set.emotion_name}")

    audio_path = output_path / "audio.wav"
    video_path = output_path / "video.mp4"
    await save_audio_with_ffmpeg(audio=audio, bps=bps, sample_rate=sample_rate, output_path=audio_path)
    await combine_with_ffmpeg(frames_dir=frames_path, audio_path=audio_path, output_path=video_path, fps=25)
    shutil.rmtree(frames_path)
    logger.info(f"Frames in path {frames_path} are deleted")
    cleanup_old_results()
    logger.info(f"Old results cleared")


async def run():
    url = "localhost:8501"
    aud_file = Path("../../samples/11sec_16k_1ch.wav")
    img_file = "tools/client/res/gomer.png"
    avatar_id = "vedenina2"
    print(avatar_id)
    output_path = Path("../../test")
    request_id = uuid4()

    with wave.open(str(aud_file), 'rb') as wf:
        n_frames =wf.getnframes()
        audio = wf.readframes(n_frames)
        sr = wf.getframerate()

    await local_video_run(audio=audio, avatar_id=avatar_id, output_path=output_path / str(request_id), sample_rate=sr, bps=16)


if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(run())
