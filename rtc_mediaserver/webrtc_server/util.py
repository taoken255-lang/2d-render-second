import asyncio
import time

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
