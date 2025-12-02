"""Shared runtime objects used across WebRTC server components."""
from __future__ import annotations

import asyncio
from queue import Queue
from typing import Tuple, List

import numpy as np

from rtc_mediaserver.config import settings

# Queues used for synchronising audio and video blocks between gRPC client
# (that produces rendered frames) and the media player that feeds them to AIORTC.
AUDIO_SECOND_QUEUE: "Queue[Tuple[np.ndarray, int]]" = Queue()
SYNC_QUEUE: "Queue[Tuple[np.ndarray, List[Tuple[np.ndarray, int]], str]]" = Queue(settings.sync_queue_chunks)
SYNC_QUEUE_SEM: "asyncio.Semaphore" = asyncio.BoundedSemaphore(2)
# Number of active viewer websocket connections
VIEWER_COUNT: int = 0

# Simple flag to prevent several clients uploading WAVs simultaneously
ACTIVE_UPLOAD: bool = False

__all__ = [
    "AUDIO_SECOND_QUEUE",
    "SYNC_QUEUE",
    "VIEWER_COUNT",
    "ACTIVE_UPLOAD",
]
