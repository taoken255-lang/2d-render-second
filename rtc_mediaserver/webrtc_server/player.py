"""Media player implementation that feeds audio & video from queues to aiortc tracks."""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections import deque
from typing import Deque, Optional, Set, Tuple, Union, List

import av  # type: ignore
import numpy as np  # type: ignore
from aiortc import MediaStreamTrack  # type: ignore
from av.frame import Frame  # type: ignore
from av.packet import Packet  # type: ignore

from rtc_mediaserver.logging_config import get_logger, setup_default_logging
from .constants import AUDIO_SETTINGS, VIDEO_CLOCK, VIDEO_PTIME, VIDEO_TB, USER_EVENTS, INTERRUPT_CALLED, STATE
from .shared import SYNC_QUEUE, SYNC_QUEUE_SEM

# Make sure logging is configured as early as possible
setup_default_logging()
logger = get_logger(__name__)

__all__ = [
    "PlayerStreamTrack",
    "WebRTCMediaPlayer",
]

TIME_SPAN = 0.02

class PlayerStreamTrack(MediaStreamTrack):
    """Custom aiortc track that pulls frames from an internal queue."""

    kind: str

    def __init__(self, player: "WebRTCMediaPlayer", kind: str):
        super().__init__()
        self.kind = kind
        self._player = player
        self._count = 0
        self._last_sent_time = 0
        # Диагностика частоты recv()
        self._recv_count = 0
        self._last_recv_time = 0
        self._recv_times = []
        # FIX сбалансированные размеры очередей для предотвращения рассинхронизации  
        # Видео: 5 кадров * 40ms = 200ms буфер
        # Аудио: 10 чанков * 20ms = 200ms буфер (тот же буфер по времени!)
        self._queue: asyncio.Queue[Tuple[Union[Frame, Packet], float, str | None]] = asyncio.Queue()  # FIX равные буферы по времени
        self._tb = VIDEO_TB if kind == "video" else AUDIO_SETTINGS.audio_tb
        self._period = VIDEO_PTIME if kind == "video" else AUDIO_SETTINGS.audio_ptime
        self._rate = VIDEO_CLOCK if kind == "video" else AUDIO_SETTINGS.sample_rate
        self._pts: int = 0
        self._start: Optional[float] = None  # in perf_counter timebase (not wallclock)  # FIX ясная семантика базы
        self._t0 = time.time()
        self.lags = 3
        self.idx = 0

    async def _sleep_until_slot(self) -> None:
        """Sleep just enough to achieve a constant frame/packet rate."""

        t0 = self._player._ensure_t0()
        if self._start is None:
            self._start = t0
            return

        self._pts += int(self._rate * self._period)
        target = self._start + self._pts / self._rate
        now = time.perf_counter()
        delay = target - now
        
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            if delay < -0.12:
                self._start = now - self._pts / self._rate  # FIX soft resync

    async def recv(self):  # type: ignore[override]
        self._player._ensure_worker(self)

        await self._sleep_until_slot()

        frame, idx, event = await self._queue.get()

        if event:
            async def send_event(evt: str):
                logging.debug(f"Send event {event}")
                USER_EVENTS.put_nowait({"type": event})
                if event == "eos":
                    STATE.zero_perf_timer()
                await asyncio.sleep(0)
            asyncio.run_coroutine_threadsafe(send_event(event), self._player.main_loop)


        frame.pts = self._pts
        frame.time_base = self._tb
        self.idx += 1

        return frame


    def stop(self) -> None:  # type: ignore[override]
        super().stop()
        if self._player:
            self._player._track_stopped(self)
            self._player = None


class WebRTCMediaPlayer:
    """Background thread that converts synced audio/video batches into aiortc frames."""

    def __init__(self) -> None:
        self._audio_track = PlayerStreamTrack(self, "audio")
        self._video_track = PlayerStreamTrack(self, "video")
        self._active: Set[PlayerStreamTrack] = set()
        self._thread: Optional[threading.Thread] = None
        self._quit = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.main_loop: Optional[asyncio.AbstractEventLoop] = None
        # Buffers for in-flight batch currently being streamed
        self._audio_chunks: Deque[List[np.ndarray, str | None]] = deque()
        self._video_frames: Deque[Tuple[np.ndarray, int]] = deque()

        # FIX единый мастер-час для обоих треков, защищённый локом
        self._t0_perf: Optional[float] = None  # perf_counter timestamp  # FIX master clock storage
        self._t0_lock = threading.Lock()  # FIX guard for t0 init

        self._last_batch_time = 0
        self._audio_tb = AUDIO_SETTINGS.audio_tb
        self._video_tb = VIDEO_TB
        self._audio_pts = 0
        self._video_pts = 0
        self._t0 = time.time()
        self.base = None
        self.next_deadline = None

        # Public tracks exposed to aiortc peer connection
    @property
    def audio(self) -> PlayerStreamTrack:  # type: ignore[override]
        return self._audio_track

    @property
    def video(self) -> PlayerStreamTrack:  # type: ignore[override]
        return self._video_track

    # ───────────────── Track/worker lifecycle helpers ──────────────────
    def _ensure_worker(self, track: PlayerStreamTrack) -> None:
        """Start background worker the first time any track is pulled."""
        self._active.add(track)
        if self._thread is None:
            self._loop = asyncio.get_running_loop()
            self._quit.clear()
            self._thread = threading.Thread(target=self._worker, name="media-player", daemon=True)
            self._thread.start()
            logger.info("media thread started")

    def _track_stopped(self, track: PlayerStreamTrack) -> None:
        self._active.discard(track)
        if not self._active and self._thread:
            self._quit.set()
            self._thread.join()
            self._thread = None
            logger.info(f"media thread stopped aqs={self._audio_track._queue.qsize()}, vqs={self._video_track._queue.qsize()}")

    # FIX метод для инициализации общего t0 на perf_counter
    def _ensure_t0(self) -> float:
        with self._t0_lock:
            if self._t0_perf is None:
                self._t0_perf = time.perf_counter()
            return self._t0_perf

    # ───────────────────── Background worker ───────────────────────────
    def _worker(self) -> None:
        AUDIO_DT = AUDIO_SETTINGS.audio_ptime  # 0.02
        VIDEO_DT = VIDEO_PTIME                # 0.04

        self.base = None
        self.next_deadline = None

        audio_sent = 0
        video_sent = 0

        while not self._quit.is_set():
            if STATE.first_chunk_received:
                if not self.base:
                    self.base = time.perf_counter()
                    self.next_deadline = self.base

                now = time.perf_counter()

                global_missed = now - self.base

                audio_chunks = int((global_missed / AUDIO_DT) - audio_sent) + 1
                video_chunks = int((global_missed / VIDEO_DT) - video_sent) + 1

                while audio_chunks:
                    if self._push_audio():
                        audio_sent += 1
                    else:
                        break
                    audio_chunks -= 1

                while video_chunks > 0:
                    if self._push_video():
                        video_sent += 1
                    else:
                        break
                    video_chunks -= 1

                self.next_deadline += 0.02
                sleep_time = self.next_deadline - time.perf_counter()
                if sleep_time > 0:
                    time.sleep(sleep_time)
            else:

                self.base = None
                self._video_frames.clear()
                self._audio_chunks.clear()
                audio_sent = 0
                video_sent = 0

    # ───────────────────── Internal helpers ────────────────────────────
    def _load_next_batch(self) -> bool:
        """Pop next synced batch (1 sec audio + 25 frames) from queue."""

        if SYNC_QUEUE.empty():
            return False

        audio_sec, frames25, evt_to_send = SYNC_QUEUE.get()

        for i in range(0, len(audio_sec), AUDIO_SETTINGS.audio_samples):
            self._audio_chunks.append([audio_sec[i:i + AUDIO_SETTINGS.audio_samples], evt_to_send if evt_to_send == "interrupted" else None])
        self._audio_chunks[-1][1] = evt_to_send

        for i in frames25:
            frame, idx = i
            self._video_frames.append((frame, idx, evt_to_send if evt_to_send == "interrupted" else None))

        return True

    def _push_audio(self) -> None:
        if not self._audio_chunks:
            self._load_next_batch()
        if not self._audio_chunks:
            logger.debug("push_audio: no chunks available")
            return False

        chunk, event = self._audio_chunks.popleft()
        if event == "interrupted":
            return True
        frame = av.AudioFrame(format="s16", layout="mono", samples=AUDIO_SETTINGS.audio_samples)
        if np.any(chunk):
            STATE.send_to_client()
        frame.planes[0].update(chunk.tobytes())
        frame.sample_rate = AUDIO_SETTINGS.sample_rate

        if self._loop:
            try:
                def put_audio(frame, idx_frame, event):
                    self._audio_track._queue.put_nowait((frame, time.perf_counter(), event))
                self._loop.call_soon_threadsafe(
                    put_audio, *(frame, None, event)
                )
            except asyncio.QueueFull:
                logger.warning("push_audio: queue full, dropping 20ms chunk")

        return True

    def _push_video(self) -> None:
        if not self._video_frames:
            if not self._load_next_batch():
                logger.debug("push_video: no frames available")
                return
        if not self._video_frames:
            logger.debug("push_video: no frames available after batch load")
            return False

        arr, frame_idx, evt = self._video_frames.popleft()
        frame_raw, w, h = arr
        if evt == "interrupted":
            return True
        #frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        frame = av.VideoFrame(w, h, "rgb24")
        frame.planes[0].update(frame_raw)
        if self._loop:
            try:
                def put_video(frame, idx, event):
                    self._video_track._queue.put_nowait((frame, frame_idx, None))
                self._loop.call_soon_threadsafe(
                    put_video,
                    *(frame, frame_idx, None)
                )
            except asyncio.QueueFull:
                logger.warning("push_video: queue full, dropping frame (A/V desync possible!)")

        return True
