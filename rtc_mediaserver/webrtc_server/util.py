import asyncio
import io
import time
import wave
from typing import Tuple, Optional

import numpy as np

from rtc_mediaserver.logging_config import get_logger
from rtc_mediaserver.webrtc_server.constants import STATE
from rtc_mediaserver.webrtc_server.handlers import ClientState
from rtc_mediaserver.webrtc_server.shared import AUDIO_SECOND_QUEUE

logger = get_logger(__name__)


async def _flush_pcm_buf(state: ClientState, t1 = None) -> None:
    """Push accumulated PCM data to AUDIO_SECOND_QUEUE as 1-sec chunks."""
    bps = state._bytes_per_chunk()
    logger.info(f"bps = {bps}")
    pcm_buf = state.pcm_buf
    while len(pcm_buf) >= bps:
        sec_bytes = pcm_buf[:bps]
        del pcm_buf[:bps]
        arr = np.frombuffer(sec_bytes, dtype=np.int16)
        AUDIO_SECOND_QUEUE.put_nowait((arr, state.sample_rate))
        if t1:
            logger.info(f"TMR 11labs chunk time =  {time.time() - STATE.tts_start}")
        await asyncio.sleep(0)
        logger.info("Queued X-second audio chunk (%d samples)", arr.shape[0])


def get_sample_rate_from_wav_bytes(wav_bytes):
    """
    Extracts the sample rate from WAV file bytes.

    Args:
        wav_bytes (bytes): The raw bytes of a WAV file.

    Returns:
        int: The sample rate of the WAV file.
    """
    try:
        # Create a file-like object from the bytes
        wav_file_object = io.BytesIO(wav_bytes)

        # Open the WAV file from the file-like object
        with wave.open(wav_file_object, 'rb') as wf:
            sample_rate = wf.getframerate()
            return sample_rate
    except wave.Error as e:
        print(f"Error reading WAV data: {e}")
        return None

def wav_to_mono_and_sample_rate(wav_bytes: bytes) -> Tuple[Optional[int], Optional[bytes]]:
    bio = io.BytesIO(wav_bytes)
    try:
        with wave.open(bio, 'rb') as wf:
            num_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            num_frames = wf.getnframes()

            data = wf.readframes(num_frames)
    except wave.Error as e:
        logger.error(f"audio decode error -> {e!r}")
        return None, None

    if sample_width != 2:
        logger.error(f"wrong sample width")
        return None, None

    if num_channels != 1:
        logger.error(f"wrong channels number")
        return None, None

    return sample_rate, data
