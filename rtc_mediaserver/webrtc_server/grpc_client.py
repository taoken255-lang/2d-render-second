"""gRPC client that streams audio to render service and receives corresponding frames."""
from __future__ import annotations

import asyncio
import glob
import logging
import random
import re
import threading
import time
import wave
from pathlib import Path
from typing import Deque, List, Tuple

import numpy as np  # type: ignore
from PIL import Image

from rtc_mediaserver.logging_config import get_logger, setup_default_logging
from .constants import DEFAULT_IMAGE_PATH, AUDIO_SETTINGS, CAN_SEND_FRAMES, \
    FRAMES_PER_CHUNK, USER_EVENTS, INTERRUPT_CALLED, COMMANDS_QUEUE, STATE, SYNTHESIZE_IN_PROGRESS
from .shared import AUDIO_SECOND_QUEUE, SYNC_QUEUE, SYNC_QUEUE_SEM
from ..config import settings
from ..events import ServiceEvents, Conditions

# Configure logging early
setup_default_logging()
logger = get_logger(__name__)

import grpc  # type: ignore
from grpc import aio # type: ignore
from rtc_mediaserver.proto import render_service_pb2_grpc, render_service_pb2  # type: ignore

async def stream_worker_aio() -> None:
    """Background task that maintains bidirectional RenderStream gRPC call."""
    logger.info("Starting gRPC connection")
    if grpc is None:
        logger.error("grpc library not available – stream worker aborted")
        return

    opts: List[Tuple[str, int]] = [
        ("grpc.max_receive_message_length", 10 * 1024 * 1024),
        ("grpc.max_send_message_length", 10 * 1024 * 1024),
    ]

    if settings.grpc_secure_channel:
        creds = grpc.ssl_channel_credentials()
        channel = aio.secure_channel(settings.grpc_server_url, credentials=creds, options=opts)
    else:
        channel = aio.insecure_channel(settings.grpc_server_url, options=opts)

    stub = render_service_pb2_grpc.RenderServiceStub(channel)
    logger.info(f"gRPC aio stream opened → {settings.grpc_server_url}")

    # Keep state between iterations
    from collections import deque
    pending_audio: Deque[Tuple[np.ndarray, int, ServiceEvents | None, bool, bool]] = deque()
    frames_batch: List[Tuple[np.ndarray, int]] = []

    CHUNK_SAMPLES = AUDIO_SETTINGS.samples_per_chunk
    chunks_sem = asyncio.Semaphore(settings.max_inflight_chunks)

    async def sender_generator():
        """Coroutine that yields RenderRequest messages."""
        try:
            avatar = STATE.avatar
            logger.info(f"Set avatar {avatar}")
            yield render_service_pb2.RenderRequest(set_avatar=render_service_pb2.SetAvatar(avatar_id=avatar),
                                                   online=True,
                                                   output_format="RGB"
                                                   )

            speech_sended = False

            while True:
                interrupted = False

                if INTERRUPT_CALLED.is_set():
                    interrupted = True
                    if settings.fast_interrupts_enabled:
                        STATE.chunks_to_skip = len(pending_audio)
                        INTERRUPT_CALLED.clear()
                        USER_EVENTS.put_nowait({"type": "interrupted"})

                event = None
                is_speech = True
                is_interrupt = False

                if AUDIO_SECOND_QUEUE.qsize() > 0:
                    #logger.info(f"AUDIO_SECOND_QUEUE.qsize={AUDIO_SECOND_QUEUE.qsize()}")
                    audio_sec, sr = AUDIO_SECOND_QUEUE.get_nowait()

                    if audio_sec is None and sr is None:
                        logger.info(f"Got EOS marker - gen silence")
                        if speech_sended:

                            speech_sended = False
                            event = ServiceEvents.EOS if not interrupted else ServiceEvents.INTERRUPT
                            logger.info(f"EVENT push from 1 {event}")
                        audio_sec, sr = np.zeros(CHUNK_SAMPLES, dtype=np.int16), AUDIO_SETTINGS.sample_rate
                        #logger.info("AUDIO_SECOND_QUEUE empty. Steady silence – idle state")
                        is_speech = False
                    else:
                        n = audio_sec.shape[0]
                        if n < CHUNK_SAMPLES:
                            pad = np.zeros(CHUNK_SAMPLES - n, dtype=np.int16)
                            audio_sec = np.concatenate([audio_sec, pad])
                        elif n > CHUNK_SAMPLES:
                            audio_sec = audio_sec[:CHUNK_SAMPLES]

                        speech_sended = True
                        is_speech = True
                        #logger.info(f"TMR Chunk got after {time.time() - STATE.tts_start}")
                else:
                    if speech_sended:
                        speech_sended = False
                        event = ServiceEvents.EOS if not interrupted else ServiceEvents.INTERRUPT
                        logger.info(f"EVENT push from 2 {event}")
                    audio_sec, sr = np.zeros(CHUNK_SAMPLES, dtype=np.int16), AUDIO_SETTINGS.sample_rate
                    #logger.info("AUDIO_SECOND_QUEUE empty. Steady silence – idle state")
                    is_speech = False

                pending_audio.append((audio_sec, sr, event, is_speech, is_interrupt))

                request = render_service_pb2.RenderRequest(
                    audio=render_service_pb2.AudioChunk(data=audio_sec.tobytes(), sample_rate=sr, bps=16, is_voice=is_speech),
                    online=True
                )

                yield request

                while COMMANDS_QUEUE.qsize() > 0 and not SYNTHESIZE_IN_PROGRESS.is_set():
                    evt, evt_payload = COMMANDS_QUEUE.get_nowait()
                    if evt == ServiceEvents.SET_ANIMATION:
                        logger.info(f"Request -> Playing animation {evt_payload}")
                        yield render_service_pb2.RenderRequest(play_animation=render_service_pb2.PlayAnimation(animation=evt_payload, auto_idle=STATE.auto_idle))
                    elif evt == ServiceEvents.SET_EMOTION:
                        logger.info(f"Request -> set emotion {evt_payload}")
                        yield render_service_pb2.RenderRequest(
                            set_emotion=render_service_pb2.SetEmotion(emotion=evt_payload))

                logger.info("Sent audio chunk to render service (pending=%d)",len(pending_audio))

                await chunks_sem.acquire()

                await asyncio.sleep(0)

            logger.info("Sender exited")
        except BaseException as e:
            logger.error(f"Sender error {e!r}")
            import traceback
            logger.error(traceback.format_tb(e.__traceback__))

    frames = 0

    logger.info("Start reading chunks")
    prev_emotion = None
    try:
        t_start = time.time()
        async for chunk in stub.RenderStream(sender_generator()):
            # if not CAN_SEND_FRAMES.is_set():
            #     logger.info("No clients - exiting receiver")
            #     break
            if chunk.WhichOneof("chunk") == "video":
                STATE.first_chunk_received = True
                frame_idx = 0
                try:
                    frame_idx = chunk.video.frame_idx
                    #logger.info(f"FRAME_RECEIVE:GRPC {frame_idx}")
                except:
                    pass
                frames += 1
                img = Image.frombytes("RGB", (chunk.video.width, chunk.video.height), chunk.video.data, "raw")
                img_np = np.asarray(
                    img,
                    np.uint8,
                )
                frames_batch.append((img_np, frame_idx))

                if len(frames_batch) == FRAMES_PER_CHUNK:
                    logger.info(f"Got 15 frames for {time.time() - t_start}, audio pending = {len(pending_audio)}")
                    chunks_sem.release()
                    if pending_audio:
                        audio_chunk, _sr, event, is_speech, is_interrupt = pending_audio.popleft()

                        #if is_speech:
                            #logger.info(f"TMR Chunk rendered after {time.time() - STATE.tts_start}")

                        event_to_send = None
                        if event and event == ServiceEvents.EOS:
                            event_to_send = "eos"

                        if not settings.fast_interrupts_enabled and event and event == ServiceEvents.INTERRUPT:
                            USER_EVENTS.put_nowait({"type": "interrupted"})
                            INTERRUPT_CALLED.clear()
                            event_to_send = None
                            logger.info(f"EVENT Interrupted, event_to_send = None")

                        if settings.fast_interrupts_enabled and STATE.chunks_to_skip > 0:
                            STATE.chunks_to_skip =- 1
                            event_to_send = "interrupted"
                            if not STATE.chunks_to_skip and INTERRUPT_CALLED.is_set():
                                INTERRUPT_CALLED.clear()

                        logger.info(f"EVENT {event_to_send}")
                        SYNC_QUEUE.put((audio_chunk, frames_batch.copy(), event_to_send))

                        #logger.info("SYNC_QUEUE +1 (size=%d)", SYNC_QUEUE.qsize())
                    else:
                        logger.warning("Render service produced %d frames but no matching audio is pending", FRAMES_PER_CHUNK)
                    frames_batch.clear()

                    t_start = time.time()

                    await asyncio.sleep(0)
            elif chunk.WhichOneof("chunk") == "start_animation":
                USER_EVENTS.put_nowait({"type": "animationStarted", "id": chunk.start_animation.animation_name})
                logger.info(f"START ANIMATION {chunk.start_animation.animation_name}")
                await asyncio.sleep(0)
            elif chunk.WhichOneof("chunk") == "end_animation":
                USER_EVENTS.put_nowait({"type": "animationEnded", "id": chunk.end_animation.animation_name})
                logger.info(f"END ANIMATION {chunk.end_animation.animation_name}")
                await asyncio.sleep(0)
            elif chunk.WhichOneof("chunk") == "avatar_set":
                USER_EVENTS.put_nowait({"type": "avatarSet", "avatarId": chunk.avatar_set.avatar_id})
                logger.info(f"AVATAR SET  {chunk.avatar_set.avatar_id}")
                await asyncio.sleep(0)
            elif chunk.WhichOneof("chunk") == "emotion_set":
                if prev_emotion and prev_emotion != chunk.emotion_set.emotion_name:
                    USER_EVENTS.put_nowait({"type": "emotionEnded", "id": prev_emotion})
                prev_emotion = chunk.emotion_set.emotion_name
                USER_EVENTS.put_nowait({"type": "emotionStarted", "id": chunk.emotion_set.emotion_name})
                logger.info(f"EMOTION SET {chunk.emotion_set.emotion_name}")
                await asyncio.sleep(0)

        # ── flush remaining frames on stream end ────────────────────────
        logger.info("start flushing")
        if frames_batch:
            logger.info("Flushing remaining %d frame(s) after stream end", len(frames_batch))
            while frames_batch:
                batch = frames_batch[:FRAMES_PER_CHUNK].copy()
                del frames_batch[:FRAMES_PER_CHUNK]
                if not pending_audio:
                    logger.warning("No pending audio for remaining frames – stopping flush")
                    break
                audio_sec, _, _, _, _ = pending_audio.popleft()
                SYNC_QUEUE.put_nowait((audio_sec, batch))
                logger.info("Final SYNC_QUEUE +1 (flush) (size=%d)", SYNC_QUEUE.qsize())

        logger.info(f"End receiving frames, total = {frames}, pending_audio={len(pending_audio)}")

    except BaseException as e:
        logger.exception(e)
        import traceback
        logger.info(traceback.format_tb(e.__traceback__))
    finally:
        while not AUDIO_SECOND_QUEUE.empty():
            AUDIO_SECOND_QUEUE.get_nowait()
        while not SYNC_QUEUE.empty():
            SYNC_QUEUE.get_nowait()

        logger.info(f"Queues cleared")

        await channel.close()

def _natural_key(p: str | Path):
    """Ключ для сортировки кадров по числам: frame_1.png, frame_10.png → 1, 10."""
    s = str(p)
    l = [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]
    return l


def _read_frames_rgb(images_glob: str, fps: int = 25) -> List[List[np.ndarray]]:
    """Читает все PNG и режет на батчи по fps. Последний батч дополняется последним кадром."""
    files = sorted(glob.glob(images_glob), key=_natural_key)
    if not files:
        raise FileNotFoundError(f"Нет кадров по шаблону: {images_glob}")

    frames: List[np.ndarray] = []
    for f in files:
        img = Image.open(f).convert("RGB")
        frames.append(np.asarray(img, dtype=np.uint8))

    batches: List[List[np.ndarray]] = []
    for i in range(0, len(frames), fps):
        batch = frames[i:i + fps]
        if len(batch) < fps:
            batch += [batch[-1]] * (fps - len(batch))  # паддинг последним кадром
        batches.append(batch)
    logger.info("Прочитано %d кадров → %d батч(ей) по %d кадров",
                len(frames), len(batches), fps)
    return batches


def _resample_int16_mono(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Простой линейный ресемплер (без зависимостей). x: int16 mono."""
    if src_sr == dst_sr:
        return x
    n_src = x.shape[0]
    n_dst = int(round(n_src * (dst_sr / src_sr)))
    # интерполяция в float
    src_t = np.linspace(0.0, 1.0, n_src, endpoint=False, dtype=np.float64)
    dst_t = np.linspace(0.0, 1.0, n_dst, endpoint=False, dtype=np.float64)
    y = np.interp(dst_t, src_t, x.astype(np.float32))
    y = np.clip(np.rint(y), -32768, 32767).astype(np.int16)
    return y


def _read_wav_mono_16k_int16(audio_path: str, target_sr: int = 16000) -> np.ndarray:
    """Читает WAV любого кол-ва каналов, 16-бит PCM; моно и 16кГц на выходе."""
    with wave.open(audio_path, "rb") as w:
        nch = w.getnchannels()
        sampwidth = w.getsampwidth()
        sr = w.getframerate()
        nframes = w.getnframes()
        raw = w.readframes(nframes)

    if sampwidth != 2:
        raise ValueError(f"Ожидался 16-бит PCM, а пришло {8 * sampwidth}-бит. Конвертни в 16-бит.")

    data = np.frombuffer(raw, dtype="<i2")  # little-endian int16
    if nch > 1:
        # downmix → mono
        data = data.reshape(-1, nch).astype(np.int32).mean(axis=1)
        data = np.clip(data, -32768, 32767).astype(np.int16)

    if sr != target_sr:
        data = _resample_int16_mono(data, sr, target_sr)

    return data  # shape: (samples,), dtype=int16, sr=target_sr


async def stream_worker_from_disk(
    audio_path: str,
    images_glob: str = "images/frame_*.png",
    fps: int = 25,
    target_sr: int = 16000,
) -> None:
    """
    Читает WAV и PNG с диска и кладёт в SYNC_QUEUE пары (audio_sec: np.int16[16000], frames25: list[np.ndarray]).

    Правила синхронизации:
      - 1 секунда аудио (16000 сэмплов) ↔ 25 кадров.
      - Если кадров больше, чем секунд аудио — аудио дополняется нулями.
      - Если аудио длиннее — хвост игнорируется.
    """
    # --- читаем и готовим данные ---
    frames_batches = _read_frames_rgb(images_glob, fps=fps)
    audio = _read_wav_mono_16k_int16(audio_path, target_sr=target_sr)

    seconds_frames = len(frames_batches)
    samples_per_sec = target_sr
    need_samples = seconds_frames * samples_per_sec

    if audio.shape[0] < need_samples:
        pad = np.zeros(need_samples - audio.shape[0], dtype=np.int16)
        audio = np.concatenate([audio, pad], axis=0)
        logger.warning(
            "Аудио короче (%d samp) чем нужно для %d сек → дополнили нулями до %d samp",
            audio.shape[0] - pad.shape[0], seconds_frames, need_samples
        )
    elif audio.shape[0] > need_samples:
        audio = audio[:need_samples]
        logger.warning(
            "Аудио длиннее чем нужно для %d сек → обрезали до %d samp",
            seconds_frames, need_samples
        )

    # --- раскладываем по секундам и публикуем в очередь ---
    for sec_idx in range(seconds_frames):
        start = sec_idx * samples_per_sec
        end = start + samples_per_sec
        audio_sec = audio[start:end]  # np.int16, 16000 samples

        frames_batch = frames_batches[sec_idx]  # list[np.ndarray], длина = fps
        SYNC_QUEUE.put((audio_sec, frames_batch, "eos"))
        logger.info("SYNC_QUEUE +1 (sec=%d/%d, size=%d)",
                    sec_idx + 1, seconds_frames, SYNC_QUEUE.qsize())
        await asyncio.sleep(0.95)

    logger.info("Загрузка с диска завершена: %d секунд/батчей отправлено.", seconds_frames)

async def stream_worker_forever() -> None:
    """Run ``stream_worker_aio`` forever, restarting it after unhandled exceptions.

    Args:
        restart_delay: Seconds to wait before restarting after a crash.
    """
    retries = 0
    initial_delay = 5
    max_delay = 60
    current_delay = initial_delay
    while True:
        try:
            await Conditions.can_process_frames()

            def run_worker_in_thread():
                t_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(t_loop)
                STATE.streamer_loop = t_loop
                async def run_worker():
                    t = asyncio.create_task(stream_worker_aio())
                    #t = asyncio.create_task(stream_worker_from_disk("11sec_16k_1ch.wav"))
                    STATE.streamer_task = t

                    await t
                try:
                    t_loop.run_until_complete(run_worker())
                finally:
                    t_loop.close()

            thread = threading.Thread(target=run_worker_in_thread)
            thread.start()

            await asyncio.get_event_loop().run_in_executor(None, thread.join)

            logger.warning("stream_worker_aio exited normally")
            retries = 0
            current_delay = initial_delay
        except BaseException as e:
            logger.exception(f"stream_worker_aio crashed {e!r}")
            delay = current_delay + random.uniform(0, current_delay * 0.1)
            logger.info(f"stream_worker_aio sleep for {delay}s")
            await asyncio.sleep(delay)
            retries += 1
            current_delay = min(current_delay * 2, max_delay)
        finally:
            STATE.kill_streamer()
