import numpy as np

from rtc_mediaserver.webrtc_server.constants import AUDIO_SETTINGS


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