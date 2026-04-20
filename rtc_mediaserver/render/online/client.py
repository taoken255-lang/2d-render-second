"""gRPC client that streams audio to render service and receives corresponding frames."""
from __future__ import annotations

import asyncio
import random
import threading
from asyncio import CancelledError
from typing import Deque, List, Tuple

import numpy as np  # type: ignore

from rtc_mediaserver.common.logging_config import get_logger, setup_default_logging

from rtc_mediaserver.common.constants import  AUDIO_SETTINGS, CAN_SEND_FRAMES, \
    FRAMES_PER_CHUNK, USER_EVENTS, COMMANDS_QUEUE, STATE, SYNTHESIZE_IN_PROGRESS
from rtc_mediaserver.common.shared import AUDIO_SECOND_QUEUE, SYNC_QUEUE
from rtc_mediaserver.common.config import settings
from rtc_mediaserver.common.events import ServiceEvents, Conditions

# Configure logging early
setup_default_logging()
logger = get_logger(__name__)

import grpc  # type: ignore
from grpc import aio # type: ignore
from rtc_mediaserver.proto import render_service_pb2_grpc, render_service_pb2  # type: ignore

async def stream_worker_aio() -> None:
    """Background task that maintains bidirectional RenderStream gRPC call."""
    logger.info("Starting gRPC connection")

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
                if not CAN_SEND_FRAMES.is_set():
                    logger.warning(f"CAN_SEND_FRAMES not locked, exitting render sender")
                    break

                await chunks_sem.acquire()

                event = None
                is_speech = True

                if AUDIO_SECOND_QUEUE.qsize() > 0:
                    audio_sec, sr = AUDIO_SECOND_QUEUE.get_nowait()

                    if audio_sec is None and sr is None:
                        logger.debug(f"Got EOS marker - gen silence")
                        if speech_sended:

                            speech_sended = False
                            event = ServiceEvents.EOS
                            logger.debug(f"EVENT push from 1 {event}")
                        audio_sec, sr = np.zeros(CHUNK_SAMPLES, dtype=np.int16), AUDIO_SETTINGS.sample_rate
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
                else:
                    audio_sec, sr = np.zeros(CHUNK_SAMPLES, dtype=np.int16), AUDIO_SETTINGS.sample_rate
                    is_speech = False

                pending_audio.append((audio_sec, sr, event))

                request = render_service_pb2.RenderRequest(
                    audio=render_service_pb2.AudioChunk(data=audio_sec.tobytes(), sample_rate=sr, bps=16, is_voice=is_speech),
                    online=True
                )

                yield request

                while COMMANDS_QUEUE.qsize() > 0 and not SYNTHESIZE_IN_PROGRESS.is_set():
                    evt, evt_payload = COMMANDS_QUEUE.get_nowait()
                    if evt == ServiceEvents.SET_ANIMATION:
                        logger.warning(f"Request -> Playing animation {evt_payload}")
                        animation, is_quick = evt_payload
                        yield render_service_pb2.RenderRequest(play_animation=render_service_pb2.PlayAnimation(animation=animation, is_quick=is_quick, auto_idle=STATE.auto_idle))
                    elif evt == ServiceEvents.SET_EMOTION:
                        logger.warning(f"Request -> set emotion {evt_payload}")
                        yield render_service_pb2.RenderRequest(
                            set_emotion=render_service_pb2.SetEmotion(emotion=evt_payload))
                    elif evt == ServiceEvents.INTERRUPT_ANIMATION:
                        logger.warning(f"Request -> interrupt animation")
                        yield render_service_pb2.RenderRequest(
                            clear_animations=render_service_pb2.ClearAnimations())
                    elif evt == ServiceEvents.INTERRUPT:
                        logger.warning(f"Request -> interrupt lipsync")
                        yield render_service_pb2.RenderRequest(
                            interrupt=render_service_pb2.Interrupt())

                logger.debug("Sent audio chunk to render service (pending=%d)",len(pending_audio))

                await asyncio.sleep(0)
        except CancelledError as e:
            raise e
        except Exception as e:
            logger.error(f"Sender error {e!r}")
            import traceback
            logger.error(traceback.format_tb(e.__traceback__))

    frames = 0

    logger.info("Start reading chunks")
    prev_emotion = None
    try:
        was_muted = False
        async for chunk in stub.RenderStream(sender_generator()):
            if not CAN_SEND_FRAMES.is_set():
                logger.warning(f"CAN_SEND_FRAMES not locked, skipping chunk")
                chunks_sem.release()
                continue
            if chunk.WhichOneof("chunk") == "video":
                STATE.first_chunk_received = True
                frame_idx: int = 0
                is_muted: bool = False

                try:
                    frame_idx = chunk.video.frame_idx
                except:
                    pass

                try:
                    is_muted = chunk.video.is_muted
                except:
                    pass

                frames_batch.append(((chunk.video.data, chunk.video.width, chunk.video.height), frame_idx))
                frames += 1

                if len(frames_batch) == FRAMES_PER_CHUNK:
                    audio_chunk, _sr, event = pending_audio.popleft()

                    logger.debug(f"{frame_idx} pending_audio.popleft() got {event}")

                    if is_muted:
                        if not was_muted:
                            USER_EVENTS.put_nowait({"type": "interrupted"})
                            logger.debug(f"{frame_idx} EVENT Interrupted from MUTED")
                            event = None
                            was_muted = True

                        audio_chunk = np.zeros(CHUNK_SAMPLES, dtype=np.int16)
                    else:
                        if was_muted:
                            was_muted = False

                    event_to_send = None
                    if event and event == ServiceEvents.EOS:
                        event_to_send = "eos"

                    logger.debug(f"EVENT {event_to_send}")
                    SYNC_QUEUE.put((audio_chunk, frames_batch.copy(), event_to_send))

                    frames_batch.clear()
                    chunks_sem.release()
                    await asyncio.sleep(0)

            elif chunk.WhichOneof("chunk") == "start_animation":
                USER_EVENTS.put_nowait({"type": "animationStarted", "id": chunk.start_animation.animation_name})
                logger.debug(f"START ANIMATION {chunk.start_animation.animation_name}")
                await asyncio.sleep(0)
            elif chunk.WhichOneof("chunk") == "end_animation":
                USER_EVENTS.put_nowait({"type": "animationEnded", "id": chunk.end_animation.animation_name})
                logger.debug(f"END ANIMATION {chunk.end_animation.animation_name}")
                await asyncio.sleep(0)
            elif chunk.WhichOneof("chunk") == "avatar_set":
                USER_EVENTS.put_nowait({"type": "avatarSet", "avatarId": chunk.avatar_set.avatar_id})
                logger.debug(f"AVATAR SET  {chunk.avatar_set.avatar_id}")
                await asyncio.sleep(0)
            elif chunk.WhichOneof("chunk") == "emotion_set":
                if prev_emotion and prev_emotion != chunk.emotion_set.emotion_name:
                    USER_EVENTS.put_nowait({"type": "emotionEnded", "id": prev_emotion})
                prev_emotion = chunk.emotion_set.emotion_name
                USER_EVENTS.put_nowait({"type": "emotionStarted", "id": chunk.emotion_set.emotion_name})
                logger.debug(f"EMOTION SET {chunk.emotion_set.emotion_name}")
                await asyncio.sleep(0)
            elif chunk.WhichOneof("chunk") == "animations_cleared":
                USER_EVENTS.put_nowait({"type": "animationsCleared"})
                logger.debug(f"Animations cleared")
                await asyncio.sleep(0)

        logger.info("End receiving frames")

    except BaseException as e:
        logger.exception(e)
        import traceback
        logger.error(traceback.format_tb(e.__traceback__))
    finally:
        while not AUDIO_SECOND_QUEUE.empty():
            AUDIO_SECOND_QUEUE.get_nowait()
        while not SYNC_QUEUE.empty():
            SYNC_QUEUE.get_nowait()

        logger.warning(f"Queues cleared")
        await channel.close()
        logger.warning(f"gRPC channel closed")


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
                    STATE.streamer_task = t
                    await t
                    logger.info("run_worker -> exitted")
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
        except Exception as e:
            logger.exception(f"stream_worker_aio crashed {e!r}")
            delay = current_delay + random.uniform(0, current_delay * 0.1)
            logger.debug(f"stream_worker_aio sleep for {delay}s")
            await asyncio.sleep(delay)
            retries += 1
            current_delay = min(current_delay * 2, max_delay)
        finally:
            STATE.kill_streamer()
