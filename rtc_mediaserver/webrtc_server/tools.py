import numpy as np

from rtc_mediaserver.config import settings
from rtc_mediaserver.logging_config import get_logger
from rtc_mediaserver.webrtc_server.constants import AUDIO_SETTINGS
import os
import shutil
from pathlib import Path

logger = get_logger(__name__)

def fit_chunk(chunk: np.ndarray, expected_samples: int = AUDIO_SETTINGS.samples_per_chunk) -> np.ndarray:
    """
    Привести аудиочанк к точной длине expected_samples.
    Недостающие сэмплы заполняются нулём.

    Args:
        chunk: 1-D numpy массив dtype=np.int16
        expected_samples: сколько сэмплов должно быть (например AUDIO_SETTINGS.audio_samples)

    Returns:
        Новый numpy массив длиной exactly expected_samples.
    """
    cur_len = len(chunk)

    if cur_len == expected_samples:
        return chunk  # уже нужной длины

    if cur_len < expected_samples:                       # дополнить
        pad = np.zeros(expected_samples - cur_len, dtype=chunk.dtype)
        return np.concatenate((chunk, pad))

    # cur_len > expected_samples – лишнее отбросим
    return chunk[:expected_samples]


def cleanup_old_results():
    base = Path(settings.offline_output_path)
    dirs = [d for d in base.iterdir() if d.is_dir()]
    if len(dirs) <= settings.offline_results_to_keep:
        logger.info(f"No results to clean")
        return

    dirs_sorted = sorted(dirs, key=lambda d: d.stat().st_mtime, reverse=True)

    to_delete = dirs_sorted[settings.offline_results_to_keep:]

    for d in to_delete:
        shutil.rmtree(d)

    logger.info(f"Cleaned results {[str(d) for d in to_delete]}")
