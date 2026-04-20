import asyncio

import numpy as np

from rtc_mediaserver.common.logging_config import get_logger
from rtc_mediaserver.api.handlers import ClientState
from rtc_mediaserver.common.shared import AUDIO_SECOND_QUEUE

logger = get_logger(__name__)


import io
import struct
import wave
from typing import Optional, Tuple

# WAV format tags
WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

# Extensible subformat GUID tails (little-endian stored)
# GUID for PCM: 00000001-0000-0010-8000-00AA00389B71
# GUID for IEEE float: 00000003-0000-0010-8000-00AA00389B71
_PCM_SUBFORMAT_PREFIX = b"\x01\x00\x00\x00"
_FLOAT_SUBFORMAT_PREFIX = b"\x03\x00\x00\x00"
_SUBFORMAT_SUFFIX = b"\x00\x00\x10\x00\x80\x00\x00\xAA\x00\x38\x9B\x71"


def _parse_wav_format_tag_and_bits(wav_bytes: bytes) -> Tuple[Optional[int], Optional[int]]:
    """
    Returns (format_tag, bits_per_sample) from the WAV 'fmt ' chunk.
    Handles PCM, IEEE float, EXTENSIBLE.
    """
    if len(wav_bytes) < 12:
        return None, None
    if wav_bytes[0:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        return None, None

    off = 12
    n = len(wav_bytes)

    while off + 8 <= n:
        chunk_id = wav_bytes[off:off+4]
        chunk_sz = struct.unpack_from("<I", wav_bytes, off + 4)[0]
        off += 8

        if off + chunk_sz > n:
            return None, None

        if chunk_id == b"fmt ":
            if chunk_sz < 16:
                return None, None

            fmt_tag, ch, sr, byte_rate, block_align, bps = struct.unpack_from("<HHIIHH", wav_bytes, off)

            # EXTENSIBLE: need subformat to know PCM vs FLOAT
            if fmt_tag == WAVE_FORMAT_EXTENSIBLE and chunk_sz >= 40:
                # cbSize (2), validBits (2), channelMask (4), subformat (16)
                # subformat is a GUID, stored in little-endian for first 3 fields
                # but easiest robust check: match the raw 16 bytes as stored.
                subformat = wav_bytes[off + 24: off + 40]
                if subformat[:4] == _PCM_SUBFORMAT_PREFIX and subformat[4:] == _SUBFORMAT_SUFFIX:
                    fmt_tag = WAVE_FORMAT_PCM
                elif subformat[:4] == _FLOAT_SUBFORMAT_PREFIX and subformat[4:] == _SUBFORMAT_SUFFIX:
                    fmt_tag = WAVE_FORMAT_IEEE_FLOAT
                else:
                    # unknown extensible subformat
                    return None, None

            return fmt_tag, bps

        # chunks are word-aligned (pad 1 byte if odd)
        off += chunk_sz + (chunk_sz & 1)

    return None, None


def _ffmpeg_audio_fmt(format_tag: int, bits_per_sample: int) -> Optional[str]:
    """
    Map WAV format tag + bits to ffmpeg raw audio input format (-f ...).
    """
    if format_tag == WAVE_FORMAT_PCM:
        if bits_per_sample == 8:
            return "u8"
        if bits_per_sample == 16:
            return "s16le"
        if bits_per_sample == 24:
            return "s24le"
        if bits_per_sample == 32:
            return "s32le"
        return None

    if format_tag == WAVE_FORMAT_IEEE_FLOAT:
        if bits_per_sample == 32:
            return "f32le"
        if bits_per_sample == 64:
            return "f64le"
        return None

    return None


def wav_to_mono_and_sample_rate(wav_bytes: bytes) -> Tuple[Optional[int], Optional[str], Optional[bytes]]:
    bio = io.BytesIO(wav_bytes)

    try:
        with wave.open(bio, "rb") as wf:
            num_channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            num_frames = wf.getnframes()
            data = wf.readframes(num_frames)  # это PCM/float ДАННЫЕ без заголовка (payload)
    except wave.Error as e:
        logger.error(f"audio decode error -> {e!r}")
        return None, None, None

    if num_channels != 1:
        logger.error("wrong channels number")
        return None, None, None

    # Определяем реальный формат (PCM vs float) и bits-per-sample
    fmt_tag, bits = _parse_wav_format_tag_and_bits(wav_bytes)
    if fmt_tag is None or bits is None:
        return None, None, None

    audio_fmt = _ffmpeg_audio_fmt(fmt_tag, bits)
    if audio_fmt is None:
        logger.error(f"unsupported wav format: tag={fmt_tag}, bits={bits}")
        return None, None, None

    logger.info(f"Audio format: {audio_fmt}, sample rate: {sample_rate}, data: {len(data)}")

    return sample_rate, audio_fmt, data

async def _flush_pcm_buf(state: ClientState, t1 = None) -> None:
    """Push accumulated PCM data to AUDIO_SECOND_QUEUE as 1-sec chunks."""
    bps = state._bytes_per_chunk()
    pcm_buf = state.pcm_buf
    while len(pcm_buf) >= bps:
        sec_bytes = pcm_buf[:bps]
        del pcm_buf[:bps]
        arr = np.frombuffer(sec_bytes, dtype=np.int16)
        AUDIO_SECOND_QUEUE.put_nowait((arr, state.sample_rate))
        await asyncio.sleep(0)
        logger.debug("Queued X-second audio chunk (%d samples)", arr.shape[0])