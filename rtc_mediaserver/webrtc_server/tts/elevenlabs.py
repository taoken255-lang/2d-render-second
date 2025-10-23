import asyncio
import base64
import json
import time
import traceback
import logging
import aiohttp
import numpy as np
import websockets

from rtc_mediaserver.config import settings
from rtc_mediaserver.logging_config import get_logger, setup_default_logging
from rtc_mediaserver.webrtc_server.constants import AUDIO_SETTINGS, SENTENCES_QUEUE, SYNTHESIZE_IN_PROGRESS, STATE
from rtc_mediaserver.webrtc_server.shared import AUDIO_SECOND_QUEUE
from rtc_mediaserver.webrtc_server.tools import fit_chunk
from rtc_mediaserver.webrtc_server.util import _flush_pcm_buf

logger = get_logger(__name__)


def create_request_body_http(tts_text, model_id, *,
                             normalization: str = "on",
                             language_code: str = "ru",
                             stability: float = None,
                             similarity: float = None,
                             speaker_boost: bool = False) -> dict:
    base_body = {
        "text": tts_text,
        "model_id": model_id,
        "apply_text_normalization": normalization,
        "language_code": language_code,
        #"optimize_streaming_latency": settings.elevenlabs_optimize
    }
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
        text: str
):
    logger.info(f"11labs all text = {text}")
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

    logger.info(f"{lines_to_send}")

    for line_text in lines_to_send:
        logger.info(f"11labs - http, text = {line_text}")
        try:
            url = (f"{settings.elevenlabs_api_url}v1/text-to-speech/{settings.elevenlabs_voice_id}/stream?output_format=pcm_{AUDIO_SETTINGS.sample_rate}")
                   #f"&optimize_streaming_latency={settings.elevenlabs_optimize}")
            headers = {
                "xi-api-key": settings.elevenlabs_api_key,
                "Content-Type": "application/json"
            }
            request_body = create_request_body_http(tts_text=line_text,
                                                    model_id=settings.elevenlabs_model_id,
                                                    stability=settings.elevenlabs_stability)

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=request_body) as resp:
                    if resp.status == 200:
                        async for chunk in resp.content.iter_chunked(16384):
                            yield chunk
                    else:
                        message = await resp.text()
                        raise Exception(message)
        except BaseException as exc:
            logger.error(f"Unknown error in elevenlabs: {exc}")
            logger.error(traceback.format_exc())

async def voices():
    logger.info(f"11labs - check voices")
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
    except BaseException as exc:
        logger.error(f"Unknown error in elevenlabs (get voices): {exc}")
        logger.error(traceback.format_exc())

async def synthesize_ws(
        text: str
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
    while True:
        try:
            text, state = await SENTENCES_QUEUE.get()
            SYNTHESIZE_IN_PROGRESS.set()
            t1 = time.time()
            STATE.tts_start = time.time()
            chunks_gen = synthesize(text) if settings.elevenlabs_type == "http" else synthesize_ws(text)
            async for chunk in chunks_gen:
                state.pcm_buf.extend(chunk)
                await _flush_pcm_buf(state, t1)
                t1 = time.time()
                SYNTHESIZE_IN_PROGRESS.clear()
                await asyncio.sleep(0)

            if state.pcm_buf:
                arr = np.frombuffer(bytes(state.pcm_buf), dtype=np.int16)
                arr = fit_chunk(arr, expected_samples=AUDIO_SETTINGS.samples_per_chunk)
                state.pcm_buf.clear()
                AUDIO_SECOND_QUEUE.put_nowait((arr, state.sample_rate))
                await asyncio.sleep(0)
                logger.info("Queued tail audio chunk (%d samples)", arr.shape[0])
            AUDIO_SECOND_QUEUE.put_nowait((None, None))
        except BaseException as e:
            logger.error(e)
        finally:
            SYNTHESIZE_IN_PROGRESS.clear()