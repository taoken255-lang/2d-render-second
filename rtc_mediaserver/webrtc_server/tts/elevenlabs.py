import asyncio
import base64
import json
import time
import traceback
import logging
from asyncio import AbstractEventLoop
from typing import Optional

import aiohttp
import numpy as np
import websockets

from rtc_mediaserver.config import settings
from rtc_mediaserver.logging_config import get_logger, setup_default_logging
from rtc_mediaserver.webrtc_server.client_state import ClientState
from rtc_mediaserver.webrtc_server.constants import AUDIO_SETTINGS, SENTENCES_QUEUE, SYNTHESIZE_IN_PROGRESS, STATE, \
    RTC_STREAM_CONNECTED
from rtc_mediaserver.webrtc_server.shared import AUDIO_SECOND_QUEUE
from rtc_mediaserver.webrtc_server.tools import fit_chunk
from rtc_mediaserver.webrtc_server.util import _flush_pcm_buf

logger = get_logger(__name__)


def create_request_body_http(tts_text, model_id, *,
                             normalization: str = "on",
                             language_code: Optional[str] = None,
                             stability: float = None,
                             similarity: float = None,
                             speaker_boost: bool = False) -> dict:
    base_body = {
        "text": tts_text,
        "model_id": model_id,
        "apply_text_normalization": normalization,
        "optimize_streaming_latency": settings.elevenlabs_optimize
    }
    if language_code:
        base_body["language_code"] = language_code
    voice_settings = {
        "speed": settings.elevenlabs_voice_speed
    }

    if stability:
        voice_settings["stability"] = stability
    if similarity:
        voice_settings["similarity_boost"] = similarity
    if speaker_boost:
        voice_settings["use_speaker_boost"] = speaker_boost
    if len(voice_settings) > 0:
        base_body["voice_settings"] = voice_settings

    return base_body

async def synthesize(
        text: str,
        lang_code: Optional[str] = None
):
    logger.debug(f"11labs all text = {text}, lang_code={lang_code}")
    lines_to_send = []
    lines = text.splitlines()
    lines = [l.strip() for l in lines]
    lines = [l for l in lines if l]

    for line in lines:
        sentences = line.split(". ")
        sentences = [s for s in sentences]
        lines_to_send.extend(sentences)

    for k, v in enumerate(lines_to_send):
        if not v.endswith('.'):
            lines_to_send[k] = f"{v}."

    logger.debug(f"{lines_to_send}")

    for line_text in lines_to_send:
        logger.debug(f"11labs - http, text = {line_text}")
        try:
            url = (f"{settings.elevenlabs_api_url}v1/text-to-speech/{settings.elevenlabs_voice_id}/stream?output_format=pcm_{AUDIO_SETTINGS.sample_rate}"
                   f"&optimize_streaming_latency={settings.elevenlabs_optimize}")
            logger.debug(f"11labs request url [{url}]")
            headers = {
                "xi-api-key": settings.elevenlabs_api_key,
                "Content-Type": "application/json"
            }
            request_body = create_request_body_http(tts_text=line_text,
                                                    model_id=settings.elevenlabs_model_id,
                                                    stability=settings.elevenlabs_stability,
                                                    language_code=lang_code)
            logger.info(f"11labs start request")
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=request_body) as resp:
                    if resp.status == 200:
                        logger.debug(f"11labs response ok")
                        async for chunk in resp.content.iter_chunked(16384):
                            logger.debug(f"11labs yield chunk")
                            yield chunk
                        yield None
                    else:
                        message = await resp.text()
                        logger.error(f"11labs error {message}")
                        raise Exception(message)
            logger.info("11labs end request")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Unknown error in 11labs: {exc}")
            logger.error(traceback.format_exc())

async def voices():
    logger.debug(f"11labs - check voices")
    try:
        url = (f"{settings.elevenlabs_api_url}v2/voices")
        headers = {
            "xi-api-key": settings.elevenlabs_api_key,
            "Content-Type": "application/json"
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return True
                else:
                    message = await resp.text()
                    raise Exception(message)
    except Exception as exc:
        logger.error(f"Unknown error in elevenlabs (get voices): {exc}")
        logger.error(traceback.format_exc())

async def synthesize_ws(
        text: str,
        lang_code: Optional[str] = None
):
    logger.info(f"11labs - WS")
    uri = f"{settings.elevenlabs_ws_url}v1/text-to-speech/{settings.elevenlabs_voice_id}/" \
          f"stream-input?model_id={settings.elevenlabs_model_id}&output_format=pcm_{AUDIO_SETTINGS.sample_rate}&" \
          f"optimize_streaming_latency={settings.elevenlabs_optimize}"

    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps(
            {
                "text": " ",
                "xi_api_key": settings.elevenlabs_api_key,
                "flush": True,
                "language_code": lang_code,
                "generation_config": {"chunk_length_schedule": [100, 200, 250, 300]},
                "optimize_streaming_latency": settings.elevenlabs_optimize,
                "apply_text_normalization": "on",
                "voice_settings": {
                    "speed": settings.elevenlabs_voice_speed
                }
            }
        ))

        await websocket.send(json.dumps({"text": text, "try_trigger_generation": True}))
        await websocket.send(json.dumps({"text": "", "try_trigger_generation": True}))

        while True:
            try:
                message = await websocket.recv()
                data = json.loads(message)
                if data.get("audio"):
                    yield base64.b64decode(data["audio"])
                elif data.get('isFinal'):
                    break

            except websockets.exceptions.ConnectionClosed:
                break

async def synthesize_worker():
    current_task = asyncio.current_task()
    current_task_id = current_task.get_name()
    logger.info(f"{current_task_id} Synthesize worker started, queue_state = {SENTENCES_QUEUE.qsize()}")
    c_state: ClientState = None
    try:

        while True:
            try:
                text, lang_code, state = await SENTENCES_QUEUE.get()
                c_state = state
                logger.info(f"{current_task_id} 11labs Synthesize worker got sentence={text}, lang_code={lang_code}")
                SYNTHESIZE_IN_PROGRESS.set()
                t1 = time.time()
                STATE.tts_start = time.time()
                chunks_gen = synthesize(text, lang_code) if settings.elevenlabs_type == "http" else synthesize_ws(text, lang_code)
                not_flush_buf = True
                async for chunk in chunks_gen:
                    if not chunk: # end-of-sentence
                        bps = state._bytes_per_chunk()
                        await _flush_pcm_buf(state)
                        if state.pcm_buf:
                            arr = np.frombuffer(bytes(state.pcm_buf), dtype=np.int16)
                            arr = fit_chunk(arr, expected_samples=AUDIO_SETTINGS.samples_per_chunk)
                            state.pcm_buf.clear()
                            AUDIO_SECOND_QUEUE.put_nowait((arr, state.sample_rate))
                        continue
                    state.pcm_buf.extend(chunk)
                    if not_flush_buf and len(state.pcm_buf) >= AUDIO_SETTINGS.sample_rate * 2:
                        continue
                    not_flush_buf = False
                    await _flush_pcm_buf(state, t1)
                    t1 = time.time()
                    SYNTHESIZE_IN_PROGRESS.clear()
                    await asyncio.sleep(0)

                if state.pcm_buf:
                    arr = np.frombuffer(bytes(state.pcm_buf), dtype=np.int16)
                    arr = fit_chunk(arr, expected_samples=AUDIO_SETTINGS.samples_per_chunk)
                    state.pcm_buf.clear()
                    AUDIO_SECOND_QUEUE.put_nowait((arr, state.sample_rate))
                    logger.debug(f"{current_task_id} Queued tail audio chunk (%d samples)", arr.shape[0])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(e)
                import traceback
                logger.error(traceback.format_tb(e.__traceback__))
            finally:
                AUDIO_SECOND_QUEUE.put_nowait((None, None))
                logger.info(f"{current_task_id} 11labs put eos mark")
                logger.info(f"{current_task_id} 11labs end sentence")
                SYNTHESIZE_IN_PROGRESS.clear()
                await asyncio.sleep(0)
    except asyncio.CancelledError:
        logger.info(f"{current_task_id} 11labs worker killed")
        if c_state:
            c_state.pcm_buf.clear()
            logger.info(f"{current_task_id} pcm_buf cleared")

async def start_synthesize_worker():
    logger.info(f"start synthesize worker")
    STATE.synthesize_task = asyncio.create_task(synthesize_worker())

async def stop_synthesize_worker():
    logger.info("stop synthesize worker")
    task = STATE.synthesize_task
    if not task:
        logger.warning("Synthesize task not started")
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        STATE.synthesize_task = None
        logger.info("stopped synthesize worker")
