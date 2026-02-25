from __future__ import annotations

import base64
import logging
import uuid
from typing import Any, Dict, Union

import numpy as np  # type: ignore

from .client_state import ClientState
from .constants import AUDIO_SETTINGS, INTERRUPT_CALLED, AVATAR_SET, \
    INIT_DONE, COMMANDS_QUEUE, STATE, SYNTHESIZE_IN_PROGRESS, SYNTHESIZE_LOCK, SENTENCES_QUEUE
from .info import info
from .shared import AUDIO_SECOND_QUEUE, SYNC_QUEUE
from .tools import fit_chunk
from .tts.elevenlabs import synthesize
from .util import _flush_pcm_buf
from ..config import settings
from ..events import ServiceEvents

logger = logging.getLogger(__name__)



# ────────────────────────── Handlers ────────────────────────────
async def handle_init(message: Dict[str, Any], state: ClientState) -> Dict[str, Any]:
    """Init handler – store sample rate and avatar, respond with ready."""
    audio = message.get("audio") or {}
    sample_rate = audio.get("sampleRate")

    if not sample_rate or sample_rate not in (16000, 24000, 48000):
        return {
          "type": "error",
          "code": "WRONG_SAMPLE_RATE",
          "message": "Wrong sample rate value, should be one of the following: 16000, 24000, 48000"
        }

    sample_rate = int(sample_rate)

    AUDIO_SETTINGS.sample_rate = sample_rate
    state.sample_rate = sample_rate

    state.session_id = str(uuid.uuid4())

    logger.info("Client init: sampleRate=%d, avatarId=%s, sessionId=%s", sample_rate, state.avatar_id, state.session_id)
    INIT_DONE.set()
    return {"type": "ready", "sessionId": state.session_id}

async def handle_audio(message: Dict[str, Any], state: ClientState) -> dict:
    """Handle incoming raw audio chunk encoded in base64."""
    if not INIT_DONE.is_set():
        return {
          "type": "error",
          "code": "NOT_CONNECTED",
          "message": "Method `connect` should be called first."
        }
    if not AVATAR_SET.is_set():
        return {
            "type": "error",
            "code": "AVATAR_IS_NOT_SET",
            "message": "Avatar is not set."
        }
    data_b64: str = message.get("data", "")
    end_flag: bool = bool(message.get("end", False))

    if not data_b64 and not end_flag:
        return {
            "type": "error",
            "code": "AUDIO_DECODING_ERROR",
            "message": "Audio decoding error. Should be valid base-64 string."
        }
    try:
        chunk_bytes = base64.b64decode(data_b64)
    except Exception as exc:  # noqa: BLE001
        return {
            "type": "error",
            "code": "AUDIO_DECODING_ERROR",
            "message": "Audio decoding error. Should be valid base-64 string."
        }

    STATE.audio_received()

    state.pcm_buf.extend(chunk_bytes)

    logger.info(f"is_last={end_flag}")
    # Flush full seconds first
    await _flush_pcm_buf(state)

    if end_flag and state.pcm_buf:
        # Flush remaining <1-sec tail, padded with zeros
        arr = np.frombuffer(bytes(state.pcm_buf), dtype=np.int16)
        arr = fit_chunk(arr, expected_samples=AUDIO_SETTINGS.samples_per_chunk)
        state.pcm_buf.clear()
        AUDIO_SECOND_QUEUE.put_nowait((arr, state.sample_rate))
        logger.info("Queued tail audio chunk (%d samples)", arr.shape[0])

    if end_flag:
        AUDIO_SECOND_QUEUE.put_nowait((None, None))

async def handle_synthesize_speech(message: Dict[str, Any], state: ClientState) -> dict:
    """Handle incoming raw audio chunk encoded in base64."""
    if not INIT_DONE.is_set():
        return {
          "type": "error",
          "code": "NOT_CONNECTED",
          "message": "Method `connect` should be called first."
        }
    if not AVATAR_SET.is_set():
        return {
            "type": "error",
            "code": "AVATAR_IS_NOT_SET",
            "message": "Avatar is not set."
        }
    text: str = message.get("text", "")
    lang_code: str = message.get("lang_code", settings.elevenlabs_default_lang)
    if not text:
        logger.warning("handle_synthesize_speech received empty text")
        return {
            "type": "error",
            "code": "UNKNOWN_ERROR",
            "message": "Unknown error occured."
        }
    SENTENCES_QUEUE.put_nowait((text, lang_code, state))


async def handle_set_avatar(message: Dict[str, Any], state: ClientState) -> dict | None:
    avatar_id = message.get("avatarId", None)

    avatars_info = await info()
    available_avatars = list(avatars_info.keys())

    if not avatar_id or avatar_id not in available_avatars:
        return {
            "type": "error",
            "code": "INVALID_AVATAR",
            "message": "Invalid Avatar."
        }
    if not INIT_DONE.is_set():
        return {
          "type": "error",
          "code": "NOT_CONNECTED",
          "message": "Connect method should be called first."
        }
    if AVATAR_SET.is_set():
        STATE.kill_streamer()
    state.avatar_id = message.get("avatarId")
    logger.info("Set avatar → %s", state.avatar_id)
    logger.info("AVATAR_SET.set()")
    STATE.avatar = avatar_id
    AVATAR_SET.set()

async def handle_play_animation(message: Dict[str, Any], state: ClientState) -> dict:
    if not INIT_DONE.is_set():
        return {
          "type": "error",
          "code": "NOT_CONNECTED",
          "message": "Method `connect` should be called first."
        }
    if not AVATAR_SET.is_set():
        return {
            "type": "error",
            "code": "AVATAR_IS_NOT_SET",
            "message": "Avatar is not set."
        }
    animation = message.get("animation")

    STATE.auto_idle = message.get("auto_idle", True)

    avatars_info = await info()

    try:
        if animation not in avatars_info[STATE.avatar]["animations"]:
            return {
                "type": "error",
                "code": "INVALID_ANIMATION",
                "message": "Invalid animation."
            }
    except Exception as e:
        logger.error(f"Error checking animation: {e}")
        return {
                "type": "error",
                "code": "INVALID_ANIMATION",
                "message": "Invalid animation."
            }

    logger.info(f"Playing animation → {animation}, with auto_idle={STATE.auto_idle}", )
    COMMANDS_QUEUE.put_nowait((ServiceEvents.SET_ANIMATION, animation))

async def handle_set_emotion(message: Dict[str, Any], state: ClientState) -> dict:
    if not INIT_DONE.is_set():
        return {
          "type": "error",
          "code": "NOT_CONNECTED",
          "message": "Method `connect` should be called first."
        }
    if not AVATAR_SET.is_set():
        return {
            "type": "error",
            "code": "AVATAR_IS_NOT_SET",
            "message": "Avatar is not set."
        }
    emotion = message.get("emotion")

    avatars_info = await info()

    try:
        if emotion not in avatars_info[STATE.avatar]["emotions"]:
            return {
              "type": "error",
              "code": "INVALID_EMOTION",
              "message": "Invalid emotion."
            }
    except Exception as e:
        logger.error(f"Error checking emotion: {e}")
        return {
              "type": "error",
              "code": "INVALID_EMOTION",
              "message": "Invalid emotion."
            }

    logger.info("Set emotion → %s", emotion)
    COMMANDS_QUEUE.put_nowait((ServiceEvents.SET_EMOTION, emotion))

async def handle_set_panel_state(message: Dict[str, Any], state: ClientState) -> dict:
    if not INIT_DONE.is_set():
        return {
          "type": "error",
          "code": "NOT_CONNECTED",
          "message": "Method `connect` should be called first."
        }
    if not AVATAR_SET.is_set():
        return {
            "type": "error",
            "code": "AVATAR_IS_NOT_SET",
            "message": "Avatar is not set."
        }
    logger.info("Set panel state")

async def handle_interrupt(message: Dict[str, Any], state: ClientState) -> dict:
    """Clear audio queue and local buffers."""
    if not INIT_DONE.is_set():
        return {
          "type": "error",
          "code": "NOT_CONNECTED",
          "message": "Method `connect` should be called first."
        }
    if not AVATAR_SET.is_set():
        return {
            "type": "error",
            "code": "AVATAR_IS_NOT_SET",
            "message": "Avatar is not set."
        }
    INTERRUPT_CALLED.set()
    state.pcm_buf.clear()
    # Drain global queue completely (non-blocking)
    while not AUDIO_SECOND_QUEUE.empty():
        try:
            AUDIO_SECOND_QUEUE.get_nowait()
            AUDIO_SECOND_QUEUE.task_done()
        except Exception:  # noqa: BLE001
            break
    logger.info("Audio queue interrupted and cleared")
    COMMANDS_QUEUE.put_nowait((ServiceEvents.INTERRUPT, None))

async def handle_interrupt_animation(message: Dict[str, Any], state: ClientState) -> Union[dict, None]:
    if not INIT_DONE.is_set():
        return {
            "type": "error",
            "code": "NOT_CONNECTED",
            "message": "Method `connect` should be called first."
        }
    if not AVATAR_SET.is_set():
        return {
            "type": "error",
            "code": "AVATAR_IS_NOT_SET",
            "message": "Avatar is not set."
        }

    logger.info(f"Interrupting animation")
    COMMANDS_QUEUE.put_nowait((ServiceEvents.INTERRUPT_ANIMATION, None))


# Map message type → handler coroutine
HANDLERS: Dict[str, Any] = {
    "connect": handle_init,
    "audio": handle_audio,
    "setAvatar": handle_set_avatar,
    "playAnimation": handle_play_animation,
    "setPanelState": handle_set_panel_state,
    "setEmotion": handle_set_emotion,
    "interrupt": handle_interrupt,
    "clearAnimations": handle_interrupt_animation,
    "synthesizeSpeech": handle_synthesize_speech
}
