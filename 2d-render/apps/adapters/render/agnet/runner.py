"""Runner for Agnet adapter - Bridge pattern orchestration.

Orchestrates the complete render pipeline:
1. Validate worker-provided input URLs
2. Download inputs (audio, photo) to staging directory
3. Preprocess image (resize, JPG conversion, validation)
4. Call Agnet gRPC service with preprocessed image + audio
5. Collect video frames from gRPC stream
6. Encode frames + audio into MP4 using ffmpeg
7. Upload result to worker-provided upload URL

Bridge Pattern: Adapter translates HTTP → gRPC → HTTP
- No local inference engine
- Stateless, CPU-only processing
- Remote Agnet service handles GPU inference

Reference:
- Finik adapter (wrapper pattern): /home/igor/repos/infinitetalk/apps/finik_adapter/runner.py
- Agnet API: /home/igor/repos/2d-render/docs/api.md
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import AsyncExitStack
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from apps.common.adapter.base_models import TransferAuth
from apps.common.adapter.io_helpers import (
    AvatarRequest,
    MaterializedAvatar,
    validate_inputs_via_range,
    fetch_inputs_streaming,
    upload_output_streaming,
    _http_stream_get,
    _auth_header_dict,
    parse_avatar_request,
)
from apps.common.adapter.stage import Stage
from apps.common.adapter.watchdog import (
    JobWatchdogHandle,
    ProgressWatchdogConfig,
    run_watchdog,
)
from apps.common.adapter.progress_tracker import ProgressTracker
from apps.common.adapter.image_preprocessor import ImagePreprocessor, ImagePreprocessorConfig
from apps.common.adapter.ffmpeg_encoder import FFmpegEncoder, FFmpegEncoderConfig

from apps.adapters.render.agnet.direct_client import DirectRenderClient


logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Configuration (Watchdog + Agnet-specific settings)
# -----------------------------------------------------------------------------
# Load watchdog configuration from environment (shared module)
# - CANCEL_GRACE_SEC: Timeout after cancel before hard-kill (default: 60s)
# - ADAPTER_PROGRESS_STALL_MINUTES: General stall timeout (default: 27min, 0 to disable)
# - WATCHDOG_TICK_SEC: Watchdog tick interval (default: 1s for fast cancel sync)
WATCHDOG_CONFIG = ProgressWatchdogConfig.from_env()

# Agnet image preprocessing configuration
# Constraints from /home/igor/repos/2d-render/docs/input.md
AGNET_IMAGE_CONFIG = ImagePreprocessorConfig(
    max_dimension=1920,      # Max width or height in pixels
    convert_to_jpg=True,     # Convert all images to JPG format
    jpg_quality=90,          # JPG quality (85-95 recommended)
    max_file_size_mb=10,     # gRPC message size limit
    ensure_even=True,        # Required for video codecs
)

# Agnet FFmpeg encoding configuration
# Standard settings for Agnet video output (25fps H.264)
AGNET_FFMPEG_CONFIG = FFmpegEncoderConfig(
    framerate=25,            # Agnet standard framerate
    video_codec="libx264",   # H.264 for universal compatibility
    video_preset="fast",     # Balance speed/quality
    audio_codec="aac",       # AAC for universal compatibility
    audio_bitrate="128k",    # Standard audio quality
    pixel_format="yuv420p",  # Universal compatibility
)


# -----------------------------------------------------------------------------
# Hard-kill callback for watchdog
# -----------------------------------------------------------------------------
def _hard_kill(reason: str) -> None:
    """Hard-kill callback for watchdog on stall detection.

    Exit code 137 = SIGKILL (128 + 9), recognized by Docker/k8s.
    Bypasses cleanup, Docker restarts container.

    Args:
        reason: Stall type ("cancel_stall" or "progress_stall")
    """
    os._exit(137)


# -----------------------------------------------------------------------------
# Runner Configuration
# -----------------------------------------------------------------------------
@dataclass
class RunnerCfg:
    """Configuration for Agnet adapter runner.

    Attributes:
        work_root: Root directory for job staging
        http_timeout: Timeout for HTTP requests (input and upload URLs)
        job_timeout_sec: Optional overall job timeout
    """
    work_root: Path
    http_timeout: float
    job_timeout_sec: Optional[float] = None


@dataclass(frozen=True)
class InferenceResult:
    """Stable runner result returned to the adapter orchestration layer."""

    output_path: Path
    render_seconds: float
    input_audio_duration_seconds: float


class RunnerFailed(RuntimeError):
    """Raised when render pipeline fails."""
    def __init__(self, message: str) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class RenderModeSelection:
    """Normalized render mode decision derived from worker params."""

    mode: str
    avatar_request: Optional[AvatarRequest]
    avatar_id: Optional[str]


# -----------------------------------------------------------------------------
# Runner (Bridge pattern - orchestrates gRPC + ffmpeg)
# -----------------------------------------------------------------------------
class Runner:
    """Agnet adapter runner - orchestrates bridge to gRPC render service.

    Responsibilities:
      - Validate worker-provided input URLs
      - Download inputs to stage directories
      - Preprocess image (resize, JPG conversion, validation)
      - Call Agnet gRPC service with preprocessed inputs
      - Collect video frames from gRPC stream
      - Encode frames + audio into MP4 using ffmpeg
      - Upload result to provided upload destination
      - Monitor progress and handle cancellation

    Bridge Pattern: No local inference engine, translates HTTP → gRPC → HTTP
    """

    def __init__(self, cfg: RunnerCfg, logger: logging.Logger, streaming_service) -> None:
        """Initialize runner with configuration.

        Args:
            cfg: Runner configuration
            logger: Logger instance for observability
        """
        self.cfg = cfg
        self.logger = logger

        # Initialize components with adapter-specific configurations
        self.image_preprocessor = ImagePreprocessor(AGNET_IMAGE_CONFIG)
        self.ffmpeg_encoder = FFmpegEncoder(AGNET_FFMPEG_CONFIG)

        self.grpc_client = DirectRenderClient(streaming_service=streaming_service)

        self.logger.info(
            "runner: initialized Agnet adapter (direct mode) - "
            "img_config=(max=%dpx, jpg=True), ffmpeg_config=(%dfps, %s)",
            AGNET_IMAGE_CONFIG.max_dimension,
            AGNET_FFMPEG_CONFIG.framerate,
            AGNET_FFMPEG_CONFIG.video_codec
        )

    def _select_render_mode(
        self,
        *,
        render_params: Optional[dict],
        photo_path: Optional[Path],
    ) -> RenderModeSelection:
        params = render_params or {}
        try:
            avatar_request = parse_avatar_request(params)
        except RuntimeError as exc:
            raise RunnerFailed(str(exc)) from exc
        avatar_id = str(params.get("avatar_id") or "").strip() or None
        if avatar_request is not None:
            mode = "avatar"
        elif avatar_id:
            mode = "avatar_id"
        elif photo_path is not None:
            mode = "photo"
        else:
            raise RunnerFailed("Either photo_path, params.avatar_id, or params.avatar is required")
        self.logger.info(
            "runner: render mode selected mode=%s avatar_id=%s avatar_name=%s photo_path_present=%s",
            mode,
            avatar_id or "-",
            avatar_request.name if avatar_request else "-",
            photo_path is not None,
        )
        return RenderModeSelection(mode=mode, avatar_request=avatar_request, avatar_id=avatar_id)

    async def validate_inputs(
        self,
        audio_url: str,
        photo_url: Optional[str],
        *,
        transfer_auth: Optional[TransferAuth] = None,
    ) -> None:
        """Validate input URLs via HTTP requests.

        Args:
            audio_url: Input URL for audio file
            photo_url: Optional input URL for photo file (None when using avatar_id)

        Raises:
            RuntimeError: If validation fails (URL inaccessible, expired, etc.)
        """
        await validate_inputs_via_range(
            audio_url,
            photo_url,
            self.cfg.http_timeout,
            transfer_auth=transfer_auth,
        )

    async def fetch_inputs(
        self,
        stage: Stage,
        audio_url: str,
        photo_url: Optional[str],
        *,
        audio_filename: Optional[str] = None,
        photo_filename: Optional[str] = None,
        transfer_auth: Optional[TransferAuth] = None,
    ) -> Dict[str, Optional[Path]]:
        """Download inputs from URLs to staging directory.

        Args:
            stage: Stage object for working directory
            audio_url: Input URL for audio file
            photo_url: Optional input URL for photo file (None when using avatar_id)

        Returns:
            Dict with 'audio' and 'photo' paths (photo may be None if using preset avatar)

        Raises:
            RuntimeError: If download fails
        """
        if photo_url is None:
            # Avatar ID mode - skip photo download, only fetch audio
            audio_path = stage.inputs / (audio_filename or "audio.wav")
            await asyncio.to_thread(
                _http_stream_get,
                audio_url,
                audio_path,
                self.cfg.http_timeout,
                _auth_header_dict(transfer_auth),
            )
            return {"audio": audio_path, "photo": None}

        # Normal mode - download both audio and photo
        return await fetch_inputs_streaming(
            stage,
            audio_url,
            photo_url,
            self.cfg.http_timeout,
            audio_filename=audio_filename or "audio.wav",
            photo_filename=photo_filename or "photo.png",
            transfer_auth=transfer_auth,
        )

    async def run_inference(
        self,
        stage: Stage,
        *,
        audio_path: Path,
        photo_path: Optional[Path],
        render_params: Optional[dict],  # Contains avatar_id for preset avatars
        on_progress: Optional[Callable[[int], None]] = None,
        cancel_event: asyncio.Event,
        job_id: str,
        prefetched_avatar: Optional[MaterializedAvatar] = None,
    ) -> InferenceResult:
        """Execute Agnet render pipeline via gRPC bridge.

        Pipeline stages (photo mode):
        1. Preprocess image (resize, JPG conversion, validation)
        2. Call Agnet gRPC service to get video frames
        3. Encode frames + audio into MP4 using ffmpeg

        Pipeline stages (avatar_id mode):
        1. Call Agnet gRPC service with avatar_id (skips image preprocessing)
        2. Encode frames + audio into MP4 using ffmpeg

        Args:
            stage: Stage object for working directory and logs
            audio_path: Path to downloaded audio file
            photo_path: Optional path to downloaded photo file (None when using avatar_id)
            render_params: Optional render parameters (may contain avatar_id for preset avatars)
            on_progress: Optional callback for progress updates (0-100)
            cancel_event: Async event for cancellation
            job_id: Job ID for logging and tracking

        Returns:
            Output path together with render-stage timing facts derived during the run

        Raises:
            asyncio.CancelledError: On cancellation
            RunnerFailed: On pipeline failure
        """
        out_path = stage.outputs / "video.mp4"

        # Pre-check: abort early if already cancelled
        if cancel_event.is_set():
            raise asyncio.CancelledError()

        # Create JobWatchdogHandle for this job (deterministic lifecycle)
        handle = JobWatchdogHandle(async_evt=cancel_event)

        # Immediately sync cancel signal if already set
        if handle.async_evt.is_set():
            handle.thread_evt.set()
            handle.note_cancel_sync()
            self.logger.info(
                "runner: job_id=%s: cancel already set, synced",
                job_id
            )

        # Shared ProgressTracker instance feeds watchdog + user callbacks
        def tracker_callback(pct: int) -> None:
            handle.mark_progress(pct)
            if on_progress:
                try:
                    on_progress(pct)
                except Exception:
                    pass

        tracker = ProgressTracker(
            callback=tracker_callback,
            job_id=job_id,
            logger=self.logger,
        )
        tracker.update(0)  # Seed baseline progress

        # Start watchdog task
        handle.watchdog_task = asyncio.create_task(
            run_watchdog(
                handle, WATCHDOG_CONFIG, job_id,
                hard_kill=_hard_kill, logger=self.logger
            )
        )

        try:
            params = render_params or {}
            selection = self._select_render_mode(render_params=params, photo_path=photo_path)

            self.logger.info(
                "runner: job_id=%s: starting Agnet render pipeline mode=%s param_keys=%s photo_path_present=%s avatar_id=%s avatar_config_present=%s",
                job_id,
                selection.mode,
                sorted(params.keys()),
                photo_path is not None,
                selection.avatar_id or "-",
                bool(selection.avatar_request and selection.avatar_request.config),
            )
            self.logger.info(
                "[AVATAR VERSION] runner.run_inference request avatar.version=%s prefetched.version=%s",
                selection.avatar_request.version if selection.avatar_request is not None and selection.avatar_request.version is not None else "-",
                prefetched_avatar.version if prefetched_avatar is not None and prefetched_avatar.version is not None else "-",
            )
            start_ts = time.time()

            # _prepare_*_render return shape:
            # image_bytes, image_width, image_height, avatar_id, avatar_version, avatar_config
            if selection.avatar_request is not None:
                image_bytes, image_width, image_height, grpc_avatar_id, avatar_version, avatar_config = (
                    await self._prepare_avatar_object_render(
                        avatar=selection.avatar_request,
                        job_id=job_id,
                        prefetched_avatar=prefetched_avatar,
                    )
                )
            elif selection.avatar_id:
                image_bytes, image_width, image_height, grpc_avatar_id, avatar_version, avatar_config = (
                    await self._prepare_avatar_id_render(
                        avatar_id=selection.avatar_id,
                        job_id=job_id,
                        prefetched_avatar=prefetched_avatar,
                    )
                )
            elif photo_path is not None:
                image_bytes, image_width, image_height, grpc_avatar_id, avatar_version, avatar_config = (
                    await self._prepare_photo_render(
                        photo_path=photo_path,
                        job_id=job_id,
                    )
                )

            # Stage 2+3: Stream gRPC frames directly to ffmpeg (constant memory)
            self.logger.info(
                "runner: job_id=%s: [2/3] Streaming direct render -> ffmpeg",
                job_id,
            )

            # Prepare audio exactly once here so runner can keep ownership of
            # bridge-side workload facts without embedding observability callbacks
            # into the gRPC client. This stays temporary until Agnet service emits
            # native workload-normalized metrics after the adapter/service merge.
            prepared_audio = await asyncio.to_thread(self.grpc_client.prepare_audio, audio_path)

            def grpc_progress(grpc_pct: float) -> None:
                tracker.update(int(grpc_pct * 100))

            # Lazy encoder: starts ffmpeg on first frame (when dimensions are known)
            async with AsyncExitStack() as stack:
                sink = None
                final_width = image_width
                final_height = image_height

                async def on_frame(frame: bytes, width: int | None, height: int | None) -> None:
                    nonlocal sink, final_width, final_height
                    if sink is None:
                        final_width = width or image_width
                        final_height = height or image_height
                        # Fallback: infer from frame size for common resolutions (avatar_id mode)
                        if not final_width or not final_height:
                            channels = 3
                            frame_bytes = len(frame)
                            for w, h in [(512, 768), (768, 512), (1280, 720), (1920, 1080),
                                         (640, 480), (1024, 768), (800, 600), (1024, 1024)]:
                                if w * h * channels == frame_bytes:
                                    final_width, final_height = w, h
                                    self.logger.warning(
                                        "runner: job_id=%s: Inferred dimensions %dx%d from frame size %d",
                                        job_id, w, h, frame_bytes
                                    )
                                    break
                        if not final_width or not final_height:
                            raise RunnerFailed("Frame dimensions are unknown")
                        sink = await stack.enter_async_context(
                            self.ffmpeg_encoder.open_stream(
                                job_id=job_id,
                                frame_width=final_width,
                                frame_height=final_height,
                                audio_path=audio_path,
                                output_path=out_path,
                                stage=stage,
                                cancel_event=cancel_event,
                                alpha=False,
                            )
                        )
                    await sink.write(frame)

                _, detected_width, detected_height = await self.grpc_client.render_stream(
                    job_id=job_id,
                    image_bytes=image_bytes,
                    image_width=image_width,
                    image_height=image_height,
                    prepared_audio=prepared_audio,
                    cancel_event=cancel_event,
                    on_frame=on_frame,
                    on_progress=grpc_progress,
                    online=False,
                    alpha=False,
                    output_format="mp4",
                    avatar_id=grpc_avatar_id,
                    version=avatar_version,
                    avatar_config=avatar_config,
                )

            if sink is None:
                raise RunnerFailed("No frames received from gRPC stream")

            frame_count = sink.frame_count
            tracker.update(100)
            elapsed = time.time() - start_ts

            if not out_path.exists():
                raise RunnerFailed(f"Output file not found: {out_path}")

            output_size_mb = out_path.stat().st_size / (1024 * 1024)

            self.logger.info(
                "runner: job_id=%s: Render pipeline completed in %.1fs - "
                "output=%s, size=%.2f MB, frames=%d",
                job_id, elapsed, out_path.name, output_size_mb, frame_count
            )

            return InferenceResult(
                output_path=out_path,
                render_seconds=elapsed,
                input_audio_duration_seconds=prepared_audio.duration_seconds,
            )

        except asyncio.CancelledError:
            self.logger.warning(
                "runner: job_id=%s: Render pipeline cancelled",
                job_id
            )
            raise

        except Exception as e:
            self.logger.exception(
                "runner: job_id=%s: Render pipeline failed - %s",
                job_id, e
            )
            raise RunnerFailed(str(e)) from e

        finally:
            # Deterministic cleanup - stop watchdog
            if handle.watchdog_task:
                handle.watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await handle.watchdog_task

    async def _prepare_photo_render(
        self,
        *,
        photo_path: Path,
        job_id: str,
    ) -> tuple[Optional[bytes], Optional[int], Optional[int], Optional[str], Optional[int], Optional[dict[str, Any]]]:
        self.logger.info(
            "runner: job_id=%s: [1/3] Preprocessing image: %s",
            job_id, photo_path.name
        )

        image_bytes, image_width, image_height = await asyncio.to_thread(
            self.image_preprocessor.preprocess,
            photo_path
        )

        self.logger.info(
            "runner: job_id=%s: Image preprocessed: %dx%d, size=%.2f KB",
            job_id, image_width, image_height, len(image_bytes) / 1024
        )
        return image_bytes, image_width, image_height, None, None, None

    async def _prepare_avatar_id_render(
        self,
        *,
        avatar_id: str,
        job_id: str,
        prefetched_avatar: Optional[MaterializedAvatar] = None,
    ) -> tuple[None, None, None, str, Optional[int], Optional[dict[str, Any]]]:
        self.logger.info(
            "runner: job_id=%s: [1/3] Using preset avatar_id=%s (skipping image preprocessing)",
            job_id, avatar_id
        )
        self.logger.info(
            "[AVATAR VERSION] runner._prepare_avatar_id_render avatar_id=%s prefetched.version=%s",
            avatar_id,
            prefetched_avatar.version if prefetched_avatar is not None and prefetched_avatar.version is not None else "-",
        )
        if prefetched_avatar is not None:
            self.logger.info(
                "runner: job_id=%s: using prepared avatar_id=%s version=%s path=%s",
                job_id,
                prefetched_avatar.name,
                prefetched_avatar.version if prefetched_avatar.version is not None else "-",
                prefetched_avatar.path,
            )
            return None, None, None, prefetched_avatar.name, prefetched_avatar.version, prefetched_avatar.config
        return None, None, None, avatar_id, None, None

    async def _prepare_avatar_object_render(
        self,
        *,
        avatar: AvatarRequest,
        job_id: str,
        prefetched_avatar: Optional[MaterializedAvatar] = None,
    ) -> tuple[None, None, None, str, Optional[int], Optional[dict[str, Any]]]:
        effective_avatar_name = avatar.name
        effective_avatar_version = avatar.version
        effective_avatar_config = avatar.config
        effective_avatar_path = None
        if prefetched_avatar is not None:
            effective_avatar_name = prefetched_avatar.name
            effective_avatar_version = prefetched_avatar.version
            effective_avatar_config = prefetched_avatar.config
            effective_avatar_path = prefetched_avatar.path
        self.logger.info(
            "runner: job_id=%s: [1/3] Using pre-materialized avatar name=%s version=%s path=%s",
            job_id,
            effective_avatar_name,
            effective_avatar_version if effective_avatar_version is not None else "-",
            effective_avatar_path or "-",
        )
        self.logger.info(
            "[AVATAR VERSION] runner._prepare_avatar_object_render effective.version=%s request.version=%s",
            effective_avatar_version if effective_avatar_version is not None else "-",
            avatar.version if avatar.version is not None else "-",
        )
        return None, None, None, effective_avatar_name, effective_avatar_version, effective_avatar_config

    async def upload_output(
        self,
        stage: Stage,
        put_url: str,
        content_type: str,
        path: Path,
        transfer_auth: Optional[TransferAuth] = None,
    ) -> None:
        """Upload output video to output upload URL.

        Args:
            stage: Stage object for working directory
            put_url: Output upload URL
            content_type: Content type (default: video/mp4)
            path: Path to output video file

        Raises:
            RuntimeError: If upload fails
        """
        await upload_output_streaming(
            stage, put_url, content_type or "video/mp4",
            path, self.cfg.http_timeout, transfer_auth=transfer_auth, logger=self.logger
        )
