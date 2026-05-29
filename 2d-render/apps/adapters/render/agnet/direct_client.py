"""Direct in-process render client - replaces gRPC transport for co-located deployments.

Calls StreamingService.render_direct() directly instead of going over the network.
Streams frames via on_frame callback instead of buffering — runner pipes them into ffmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Optional, Callable, Tuple

from pydub import AudioSegment

from service.streaming import StreamingService
from service.object_models import (
    IPCObject, IPCDataType, ImageObject, AudioObject, EventObject, ErrorObject,
    CommandObject, CommandDataType,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedAudio:
    """Drop-in shape for the bridge's PreparedAudio.

    Holds AudioObjects (in-process IPC type) instead of pb2.AudioChunk
    (gRPC type) — runner.py only touches `.duration_seconds` and passes
    the whole object back through `render_stream`, so the chunk type
    can differ between the two render-client implementations.
    """
    chunks: tuple
    duration_seconds: float


def _load_audio_chunks(
    audio_path: Path,
    sample_rate: int = 16000,
    chunk_duration_ms: int = 1000,
) -> Tuple[list, float]:
    audio = AudioSegment.from_file(str(audio_path))
    audio = audio.set_frame_rate(sample_rate).set_channels(1).set_sample_width(2)

    duration_seconds = len(audio) / 1000.0
    chunks = []
    for i in range(0, len(audio), chunk_duration_ms):
        chunk = audio[i:i + chunk_duration_ms]
        chunks.append(AudioObject(
            data=chunk.raw_data,
            sample_rate=sample_rate,
            bps=16,
            is_voice=True,
        ))
    return chunks, duration_seconds


class DirectRenderClient:
    """In-process render client with same interface as AgnetGrpcClient.

    runner.py uses this identically — just swap the import.
    """

    def __init__(self, streaming_service: StreamingService):
        self._service = streaming_service
        self.logger = logging.getLogger(__name__)

    def prepare_audio(
        self,
        audio_path: Path,
        sample_rate: int = 16000,
        chunk_duration_ms: int = 1000,
    ) -> PreparedAudio:
        """Load audio once and return transport-ready chunks with duration.

        Drop-in for AgnetGrpcClient.prepare_audio so runner.py doesn't care
        which client it has. Runner uses `.duration_seconds` for metrics and
        passes the whole object back to `render_stream`.
        """
        chunks, duration_seconds = _load_audio_chunks(
            audio_path, sample_rate=sample_rate, chunk_duration_ms=chunk_duration_ms
        )
        self.logger.info(
            "[DirectRenderClient] prepared audio %s duration=%.2fs chunks=%d",
            audio_path.name, duration_seconds, len(chunks),
        )
        return PreparedAudio(chunks=tuple(chunks), duration_seconds=duration_seconds)

    async def render_stream(
        self,
        job_id: str,
        image_bytes: Optional[bytes],
        image_width: Optional[int],
        image_height: Optional[int],
        prepared_audio: PreparedAudio,
        cancel_event: asyncio.Event,
        on_progress: Optional[Callable[[float], None]] = None,
        on_frame: Optional[Callable[[bytes, int, int], Awaitable[None]]] = None,
        online: bool = False,
        alpha: bool = False,
        output_format: str = "RGB",
        avatar_id: Optional[str] = None,
        version: Optional[int] = None,
        avatar_config: Optional[dict] = None,
    ) -> Tuple[int, Optional[int], Optional[int]]:
        self.logger.info("[DirectRenderClient] job_id=%s: starting direct render", job_id)
        self.logger.info(
            "[AVATAR VERSION] direct_client.render_stream avatar_id=%s version=%s",
            avatar_id or "-",
            version if version is not None else "-",
        )

        audio_chunks = prepared_audio.chunks

        # get_running_loop() is the modern idiom inside coroutines;
        # get_event_loop() is deprecated as a discovery API since 3.10.
        loop = asyncio.get_running_loop()
        # asyncio.Event is not thread-safe — use threading.Event for cancel bridge
        thread_cancel = threading.Event()

        async def _watch_cancel():
            await cancel_event.wait()
            thread_cancel.set()

        watch_task = asyncio.create_task(_watch_cancel())

        # asyncio.Queue bridges the background thread → async consumer
        result_queue: asyncio.Queue = asyncio.Queue()

        def on_response(item):
            # Called from background thread — post into asyncio queue thread-safely
            loop.call_soon_threadsafe(result_queue.put_nowait, item)

        def feed_fn(audio_queue, is_online, sampling_timestamps, audio_ms_total, progress_tracker):
            # Init dict expected by start_render_process
            audio_queue.put({"is_online": online})

            # Setup: image or avatar
            if image_bytes is not None:
                audio_queue.put(IPCObject(
                    data_type=IPCDataType.IMAGE,
                    data=ImageObject(data=image_bytes, width=image_width, height=image_height),
                ))
            else:
                # Avatar-mode setup. Per docs/avatar-management/asset-examples.md
                # and the service.streaming SetAvatar handler (reads `version`,
                # `idle_name`, `agnet_config_override`, `agnet_config_merge_mode`):
                #
                #   - "avatar"  = the compositor bundle (video + config + timeline)
                #   - "config"  = ONE asset kind inside that bundle (ditto.json:
                #                  sampling_timesteps, fade_type, emo, …)
                #
                # The runner returns the resolved config dict as `avatar_config`.
                # It must land under additional_data["agnet_config_override"] so
                # the engine merges it on top of its baseline, not at the top of
                # additional_data (where streaming.py would ignore it).
                #
                # Preset-avatar-id path: avatar_config=None → default idle only.
                additional_data: dict = {"idle_name": "idle"}
                if version is not None:
                    additional_data["version"] = int(version)
                if avatar_config:
                    additional_data["agnet_config_override"] = avatar_config
                self.logger.info(
                    "[AVATAR VERSION] direct_client.feed_fn avatar_id=%s additional_data.version=%s",
                    avatar_id or "default",
                    additional_data.get("version", "-"),
                )
                audio_queue.put(IPCObject(
                    data_type=IPCDataType.COMMAND,
                    data=CommandObject(
                        command_type=CommandDataType.SetAvatar,
                        command_data=avatar_id or "default",
                        additional_data=additional_data,
                    ),
                ))

            # Stream audio chunks
            for chunk in audio_chunks:
                if thread_cancel.is_set():
                    break
                chunk_bytes = len(chunk.data)
                bps_bytes = chunk.bps // 8
                if bps_bytes > 0:
                    chunk_sum = chunk_bytes / (bps_bytes * chunk.sample_rate)
                    if progress_tracker and not is_online.is_set():
                        from service.streaming import TARGET_FPS
                        progress_tracker.total_frames += int(chunk_sum * TARGET_FPS)
                    if audio_ms_total is not None:
                        audio_ms_total.value += chunk_sum * 1000
                audio_queue.put(IPCObject(data_type=IPCDataType.AUDIO, data=chunk))

            # Sentinel — signals end of audio to the render pipeline
            audio_queue.put(None)
            # Do NOT post None to result_queue here — frames haven't arrived yet.
            # run_render() posts the sentinel after render_direct() returns.

        def run_render():
            exc = None
            try:
                self._service.render_direct(
                    feed_fn=feed_fn,
                    on_response=on_response,
                    cancel_event=thread_cancel,
                )
            except Exception as e:
                self.logger.exception("[DirectRenderClient] job_id=%s: render_direct failed: %s", job_id, e)
                exc = e
            finally:
                # Signal stream end; carry exception so the async consumer can re-raise it
                loop.call_soon_threadsafe(result_queue.put_nowait, exc)

        # Run blocking render_direct in thread pool
        render_future = loop.run_in_executor(None, run_render)

        detected_width: Optional[int] = None
        detected_height: Optional[int] = None
        frame_count = 0

        try:
            while True:
                item = await result_queue.get()

                if isinstance(item, Exception):
                    # render_direct raised — propagate with original context
                    raise item

                if item is None:
                    # Stream finished normally
                    break

                if isinstance(item, ImageObject):
                    if item.data is None:
                        break
                    if detected_width is None and item.width:
                        detected_width = item.width
                        detected_height = item.height
                        self.logger.info(
                            "[DirectRenderClient] job_id=%s: frame dimensions %dx%d",
                            job_id, detected_width, detected_height,
                        )
                    if on_frame is not None:
                        await on_frame(item.data, item.width or detected_width or 0, item.height or detected_height or 0)
                    frame_count += 1
                    if frame_count % 25 == 0:
                        self.logger.debug(
                            "[DirectRenderClient] job_id=%s: received frame %d",
                            job_id, frame_count,
                        )

                elif isinstance(item, ErrorObject):
                    self.logger.error(
                        "[DirectRenderClient] job_id=%s: error type=%s message=%s",
                        job_id, item.error_type, item.error_message,
                    )
                    raise RuntimeError(f"Render error ({item.error_type}): {item.error_message}")

                elif isinstance(item, EventObject):
                    self.logger.debug(
                        "[DirectRenderClient] job_id=%s: event %s",
                        job_id, item.event_name,
                    )

                else:
                    # Metadata / progress object from progress_tracker
                    if on_progress and hasattr(item, 'status'):
                        status = item.status
                        if hasattr(status, 'progress_percent') and status.HasField('progress_percent'):
                            on_progress(status.progress_percent / 100.0)

        except asyncio.CancelledError:
            thread_cancel.set()
            raise
        finally:
            watch_task.cancel()
            # Swallow the expected CancelledError from watch_task; also wait
            # for the render thread to finish so we don't leak it. If
            # render_direct() ignores thread_cancel and hangs, this gather
            # will block until the worker-side watchdog hard-kills the
            # container (CANCEL_GRACE_SEC); cooperative cancel inside
            # render_direct is required for clean termination.
            await asyncio.gather(watch_task, render_future, return_exceptions=True)

        if frame_count == 0:
            # Distinguish cancel from a real engine error: if the cancel
            # signal was raised, the empty stream is expected — surface it
            # as CancelledError so the runner's cancel branch updates the
            # adapter's state/metrics to "cancelled" (matches worker view)
            # instead of "failed".
            if cancel_event.is_set() or thread_cancel.is_set():
                raise asyncio.CancelledError()
            raise RuntimeError("render_direct returned no video frames")

        self.logger.info(
            "[DirectRenderClient] job_id=%s: complete, %d frames (%dx%d)",
            job_id, frame_count, detected_width or 0, detected_height or 0,
        )
        # 3-tuple matches AgnetGrpcClient.render_stream: (frame_count, width, height).
        # runner.py destructures as `_, detected_width, detected_height = ...`.
        return frame_count, detected_width, detected_height
