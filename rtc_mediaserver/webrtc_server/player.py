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

    async def _sleep_until_slot(self) -> None:
        """Sleep just enough to achieve a constant frame/packet rate."""

        t0 = self._player._ensure_t0()
        if self._start is None:
            self._start = t0
            return

        self._pts += int(self._rate * self._period)
        target = self._start + self._pts / self._rate
        now = time.perf_counter()  # FIX perf_counter вместо time.time()
        delay = target - now

        # if self._recv_count % 50 == 0:
        #     expected_time = self._start + (self._recv_count * self._period)
        #     drift = now - expected_time
        #     logger.info(f"🕐 {self.kind} SLOT: target={target:.6f} now={now:.6f} delay={delay*1000:.2f}ms "
        #               f"drift={drift*1000:.2f}ms pts={self._pts} recv#{self._recv_count}")
        
        if delay > 0:
            await asyncio.sleep(delay)
            # if delay > 0.05 and self._recv_count % 10 == 0:  # >50ms
            #     logger.warning(f"{self.kind} LONG SLEEP: {delay*1000:.1f}ms")
        else:
            # FIX мягкая ресинхронизация, если сильно опоздали (например, >120 мс):
            # подтягиваем базу, чтобы не копить постоянное отставание
            if delay < -0.12:
                old_start = self._start
                self._start = now - self._pts / self._rate  # FIX soft resync

    async def recv(self):  # type: ignore[override]
        import time
        recv_start = time.perf_counter()

        # Диагностика частоты recv()
        self._recv_count += 1
        if self._last_recv_time > 0:
            interval = recv_start - self._last_recv_time
            self._recv_times.append(interval)
            # Хранить только последние 20 интервалов
            if len(self._recv_times) > 20:
                self._recv_times.pop(0)
        self._last_recv_time = recv_start
        
        self._player._ensure_worker(self)

        # FIX КРИТИЧНО: восстанавливаем синхронизацию!
        sleep_start = time.perf_counter()
        await self._sleep_until_slot()
        sleep_duration = time.perf_counter() - sleep_start
        
        # 🔍 Проверяем состояние очереди
        queue_size = self._queue.qsize()

        frame, idx, event = await self._queue.get()

        if event:
            async def send_event(evt: str):
                logging.info(f"Send event {event}")
                USER_EVENTS.put_nowait({"type": event})
                if event == "eos":
                    STATE.zero_perf_timer()
                await asyncio.sleep(0)
            asyncio.run_coroutine_threadsafe(send_event(event), self._player.main_loop)


        # if self.kind == "video":
        #     logger.info(f"FRAME_RECEIVE:WEBRTC_RECV {idx}")

        frame.pts = self._pts
        frame.time_base = self._tb

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
            logger.info("media thread stopped")

    # FIX метод для инициализации общего t0 на perf_counter
    def _ensure_t0(self) -> float:
        with self._t0_lock:
            if self._t0_perf is None:
                self._t0_perf = time.perf_counter()
            return self._t0_perf

    # ───────────────────── Background worker ───────────────────────────
    def _worker(self) -> None:
        # FIX перешли на расписание по дедлайнам (каждые 20 мс для аудио и 40 мс для видео)
        # вместо loop_idx%2 — устойчиво к долгим итерациям и системным скачкам.
        AUDIO_DT = AUDIO_SETTINGS.audio_ptime  # 0.02
        VIDEO_DT = VIDEO_PTIME                # 0.04

        base = None
        next_deadline = None  # FIX: Следующий дедлайн для стабильного timing

        audio_sent = 0
        video_sent = 0

        while not self._quit.is_set():
            if STATE.first_chunk_received:
                if not base:
                    #logger.info(f"_worker_dbg init base")
                    base = time.perf_counter()
                    next_deadline = base  # FIX: Инициализируем первый дедлайн

                now = time.perf_counter()

                global_missed = now - base

                audio_chunks = int((global_missed / AUDIO_DT) - audio_sent) + 1
                video_chunks = int((global_missed / VIDEO_DT) - video_sent) + 1

                #logger.info(f"_worker BEFORE audio_chunks={audio_chunks}, video_chunks={video_chunks}")

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

                #logger.info(f"_worker_dbg audio_chunks={audio_chunks}, video_chunks={video_chunks}")

                # FIX: Deadline-based sleep для стабильных 20ms интервалов
                # Вместо фиксированного sleep(0.02) спим ДО следующего дедлайна
                next_deadline += 0.02
                sleep_time = next_deadline - time.perf_counter()
                if sleep_time > 0:
                    time.sleep(sleep_time)
            else:
                #logger.info(f"_worker_dbg clear base")
                base = None
                self._video_frames.clear()
                self._audio_chunks.clear()
                audio_sent = 0
                video_sent = 0
                # Если sleep_time <= 0 (опоздали) - не спим, компенсируем на следующей итерации

            # if now >= next_audio:
            #     #self._push_audio()
            #     missed = ((now - next_audio) / AUDIO_DT)
            #     # next_audio += (missed + 1) * AUDIO_DT
            #     while missed > 0:
            #         self._push_audio()
            #         missed -= AUDIO_DT
            #     next_audio += AUDIO_DT
            #
            # if now >= next_video:
            #     #self._push_video()
            #     missed = ((now - next_video) / VIDEO_DT)
            #     # next_video += (missed + 1) * VIDEO_DT
            #     while missed > 0:
            #         self._push_video()
            #         missed -= VIDEO_DT
            #     next_video += VIDEO_DT
            #
            # sleep = min(next_audio, next_video) - now
            # if sleep > 0:
            #     time.sleep(sleep)
            # else:
            #     time.sleep(0.001)

    # ───────────────────── Internal helpers ────────────────────────────
    def _load_next_batch(self) -> bool:
        """Pop next synced batch (1 sec audio + 25 frames) from queue."""
        from rtc_mediaserver.logging_config import get_logger

        if SYNC_QUEUE.empty():
            return False

        audio_sec, frames25, evt_to_send = SYNC_QUEUE.get()

        # FIX КРИТИЧНО: правильное разбиение на чанки!
        # Нарезаем 1 сек аудио на 50 чанков по 20мс
        for i in range(0, len(audio_sec), AUDIO_SETTINGS.audio_samples):
            self._audio_chunks.append([audio_sec[i:i + AUDIO_SETTINGS.audio_samples], evt_to_send if evt_to_send == "interrupted" else None])
        self._audio_chunks[-1][1] = evt_to_send

        # Добавляем 25 видео кадров
        for i in frames25:
            frame, idx = i
            self._video_frames.append((frame, idx, evt_to_send if evt_to_send == "interrupted" else None))

        get_logger(__name__).info(
            f"Loaded synced batch: {len(self._audio_chunks)} audio chunks, {len(self._video_frames)} video frames"
        )

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
                self._loop.call_soon_threadsafe(
                    self._audio_track._queue.put_nowait,
                    (frame, time.perf_counter(), event)
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
            
        # FIX проверка синхронизации буферов
        audio_count = len(self._audio_chunks)
        video_count = len(self._video_frames)
        if audio_count == 0 and video_count > 10:
            logger.warning("🚨 DESYNC: Video buffer has %d frames but audio buffer empty!", video_count)
        elif video_count == 0 and audio_count > 20:
            logger.warning("🚨 DESYNC: Audio buffer has %d chunks but video buffer empty!", audio_count)

        arr, frame_idx, evt = self._video_frames.popleft()
        if evt == "interrupted":
            return True
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        if self._loop:
            try:
                # FIX неблокирующая доставка; при переполнении — дроп кадра (видео догонит само)
                self._loop.call_soon_threadsafe(
                    self._video_track._queue.put_nowait,
                    (frame, frame_idx, None),
                )
            except asyncio.QueueFull:
                logger.warning("push_video: queue full, dropping frame (A/V desync possible!)")

        return True
