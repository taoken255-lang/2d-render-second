"""Constants for WebRTC media server components."""
from __future__ import annotations

import asyncio
import fractions
import logging
import os
import threading
import time
from pathlib import Path

__all__ = [
    "AUDIO_SETTINGS",
    "VIDEO_CLOCK",
    "VIDEO_PTIME",
    "VIDEO_TB",
    "BASE_DIR",
    "AUDIO_FILE",
    "FRAMES_DIR",
    "DEFAULT_IMAGE_PATH",
    "AUDIO_CHUNK_MS",
    "FRAMES_PER_CHUNK",
]

from aiortc import RTCPeerConnection

from rtc_mediaserver.logging_config import get_logger
from rtc_mediaserver.webrtc_server.shared import SYNC_QUEUE, SYNC_QUEUE_SEM, AUDIO_SECOND_QUEUE


class AudioParams:
    """Container for audio-related parameters that may change at runtime."""

    def __init__(self) -> None:
        # Will be updated once WAV header is parsed on upload
        self.sample_rate: int = 16000

    # 20 ms – fixed packetization time expected by the client
    @property
    def audio_ptime(self) -> float:
        return 0.020

    @property
    def audio_samples(self) -> int:
        """Number of PCM samples in a single 20 ms chunk."""
        return int(self.audio_ptime * self.sample_rate) if self.sample_rate else 0

    @property
    def samples_per_chunk(self) -> int:
        """Samples corresponding to configured FRAMES_PER_CHUNK."""
        duration = AUDIO_CHUNK_MS / 1000.0  # seconds
        return int(duration * self.sample_rate)

    @property
    def audio_tb(self) -> fractions.Fraction:  # type: ignore[override]
        """Time-base for audio frames."""
        return fractions.Fraction(1, self.sample_rate) if self.sample_rate else fractions.Fraction(0, 1)


AUDIO_SETTINGS = AudioParams()

# ── New chunk settings based on audio duration ──────────────────────
# Duration of minimal audio chunk required by gRPC (milliseconds)
AUDIO_CHUNK_MS: int = int(os.getenv("AUDIO_CHUNK_MS", "600"))  # 0.6 seconds
# -------------------------------------------------------------------

# Video constants
VIDEO_CLOCK = 90_000          # RTP 90 kHz clock
VIDEO_PTIME = 0.040           # 25 fps → 40 ms
VIDEO_TB = fractions.Fraction(1, VIDEO_CLOCK)

# Frames per chunk derived from video packetization time (40 ms → 25 fps)
FRAMES_PER_CHUNK: int = int(round(AUDIO_CHUNK_MS / (VIDEO_PTIME * 1000)))  # e.g., 600/40 = 15

# Filesystem paths
BASE_DIR = Path(__file__).resolve().parent.parent
AUDIO_FILE = BASE_DIR / "sample.wav"
FRAMES_DIR = BASE_DIR / "mock" / "images"
DEFAULT_IMAGE_PATH = str((BASE_DIR.parent / os.getenv("DEFAULT_IMAGE", "img3.png")).resolve())

CAN_SEND_FRAMES = asyncio.Event()
AVATAR_SET = asyncio.Event()
INIT_DONE = asyncio.Event()

WS_CONTROL_CONNECTED = asyncio.BoundedSemaphore(1)
RTC_STREAM_CONNECTED = asyncio.BoundedSemaphore(1)

CLIENT_COMMANDS = asyncio.Queue()
USER_EVENTS = asyncio.Queue()

INTERRUPT_CALLED = asyncio.Event()
ANIMATION_CALLED = asyncio.Event()
EMOTION_CALLED = asyncio.Event()
SYNTHESIZE_IN_PROGRESS = asyncio.Event()
SYNTHESIZE_LOCK = asyncio.Lock()

COMMANDS_QUEUE = asyncio.Queue()

SENTENCES_QUEUE = asyncio.Queue()

logger = get_logger(__name__)

class State:
    def __init__(self):
        self.streamer_task: asyncio.Task = None
        self.streamer_loop = asyncio.AbstractEventLoop = None
        self.avatar: str = None
        self.current_session_id = None
        self.auto_idle: bool = True
        self.current_pc: RTCPeerConnection = None
        self.chunks_to_skip: int = 0
        self.tts_start: float = 0
        self.first_chunk_received: bool = False
        self.perf_t: float = None
        self.first_call_r = True
        self.first_call_s = True

    def kill_streamer(self):
        if self.streamer_task:
            if not self.streamer_task.cancelled() and not self.streamer_task.done():
                try:
                    logging.info("streamer_task cancelling")
                    self.streamer_loop.call_soon_threadsafe(self.streamer_task.cancel)
                    logging.info("streamer_task cancelled")
                    self.streamer_task = None
                    self.streamer_loop = None
                except:
                    logging.error("Error cancel streamer_task")

        # while True:
        #     try:
        #         SYNC_QUEUE_SEM.release()
        #     except ValueError as e:
        #         break

        while not SYNC_QUEUE.empty():
            try:
                SYNC_QUEUE.get_nowait()
                SYNC_QUEUE.task_done()
            except Exception:  # noqa: BLE001
                break

        while not AUDIO_SECOND_QUEUE.empty():
            try:
                AUDIO_SECOND_QUEUE.get_nowait()
                AUDIO_SECOND_QUEUE.task_done()
            except Exception:  # noqa: BLE001
                break

        while not SENTENCES_QUEUE.empty():
            try:
                SENTENCES_QUEUE.get_nowait()
                SENTENCES_QUEUE.task_done()
            except Exception:  # noqa: BLE001
                break

        STATE.first_chunk_received = False

    def audio_received(self):
        if not self.perf_t:
            self.perf_t = time.time()

    def audio_rendered(self):
        if self.perf_t and self.first_call_r:
            logger.info(f"PERF_T {time.time() - self.perf_t}")
            self.first_call_r = False

    def send_to_client(self):
        if self.perf_t and self.first_call_s:
            logger.info(f"PERF_T {time.time() - self.perf_t}")
            self.first_call_s = False

    def zero_perf_timer(self):
        self.perf_t = None
        self.first_call_r = True
        self.first_call_s = True

STATE = State()