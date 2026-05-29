"""Universal FFmpeg encoder for combining raw video frames with audio.

Encodes raw RGB/RGBA frames into video format with audio muxing using ffmpeg.
Adapter-agnostic - each adapter defines its own configuration.

Usage:
    async with encoder.open_stream(job_id, width, height, audio_path, output_path, stage, cancel) as sink:
        async for frame in grpc_stream:
            await sink.write(frame)

Reference:
- Streaming pattern: /home/igor/repos/2d-render/tools/client/video/helpers/stream2video/stream2video.py
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

from apps.common.adapter.stage import Stage


@dataclass
class FFmpegEncoderConfig:
    """Configuration for FFmpeg encoding.

    Pure data container - no adapter-specific knowledge.
    Each adapter defines its own configuration instance.

    Attributes:
        framerate: Output video framerate in fps (default: 25)
        video_codec: Video codec - "libx264" (H.264), "libx265" (H.265), etc. (default: libx264)
        video_preset: Encoding speed preset - ultrafast/fast/medium/slow/veryslow (default: fast)
        video_crf: Constant Rate Factor for quality (0-51, lower=better, optional)
        video_bitrate: Target video bitrate (e.g., "2M", "5M", optional)
        audio_codec: Audio codec - "aac", "mp3", "opus", etc. (default: aac)
        audio_bitrate: Audio bitrate (e.g., "128k", "256k", default: 128k)
        pixel_format: Output pixel format - "yuv420p" for compatibility (default: yuv420p)
        container_format: Output container - "mp4", "webm", "mkv", etc. (default: mp4)
    """

    # Video settings
    framerate: int = 25
    video_codec: str = "libx264"
    video_preset: str = "fast"
    video_crf: Optional[int] = None
    video_bitrate: Optional[str] = None

    # Audio settings
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"

    # Format settings
    pixel_format: str = "yuv420p"
    container_format: str = "mp4"

    def __post_init__(self):
        """Validate configuration parameters."""
        if self.framerate <= 0:
            raise ValueError(f"framerate must be positive, got {self.framerate}")

        if self.video_crf is not None and not (0 <= self.video_crf <= 51):
            raise ValueError(f"video_crf must be 0-51, got {self.video_crf}")

        if self.video_crf is not None and self.video_bitrate:
            raise ValueError("Cannot specify both video_crf and video_bitrate (mutually exclusive)")


class RunnerFailed(Exception):
    """Raised when ffmpeg encoding fails."""
    pass


class FrameSink:
    """Write handle for streaming frames to ffmpeg stdin.

    Returned by FFmpegEncoder.open_stream(). Write one frame at a time
    for constant memory usage regardless of video length.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        expected_frame_size: int,
        job_id: str,
        logger: logging.Logger,
        cancel_event: asyncio.Event,
    ):
        self._proc = proc
        self._expected_frame_size = expected_frame_size
        self._job_id = job_id
        self._logger = logger
        self._cancel_event = cancel_event
        self._frame_count = 0
        self._bytes_written = 0

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    async def write(self, frame: bytes) -> None:
        """Write a single raw frame to ffmpeg stdin."""
        if len(frame) != self._expected_frame_size:
            self._logger.warning(
                "[FrameSink] job_id=%s: Frame %d size mismatch - expected %d, got %d",
                self._job_id, self._frame_count, self._expected_frame_size, len(frame)
            )

        try:
            self._proc.stdin.write(frame)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as e:
            # RuntimeError: uvloop raises this for closed transport instead of BrokenPipeError
            if isinstance(e, RuntimeError) and "closed" not in str(e).lower():
                raise
            if self._cancel_event.is_set():
                raise asyncio.CancelledError(
                    f"ffmpeg pipe closed during frame {self._frame_count}"
                )
            raise BrokenPipeError(
                f"ffmpeg stdin closed during frame {self._frame_count}"
            ) from e
        self._frame_count += 1
        self._bytes_written += len(frame)

        if self._frame_count % 25 == 0:
            self._logger.debug(
                "[FrameSink] job_id=%s: Streamed %d frames (%.1f MB)",
                self._job_id, self._frame_count,
                self._bytes_written / (1024 * 1024)
            )


class FFmpegEncoder:
    """Universal FFmpeg encoder for video + audio.

    Usage: open_stream() context manager for constant-memory streaming.
    """

    def __init__(self, config: FFmpegEncoderConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)

    def _build_cmd(
        self,
        frame_width: int,
        frame_height: int,
        audio_path: Path,
        output_path: Path,
        alpha: bool,
    ) -> list[str]:
        """Build ffmpeg command for raw video stdin + audio file muxing."""
        input_pixel_format = 'rgba' if alpha else 'rgb24'

        cmd = [
            'ffmpeg', '-y',
            '-f', 'rawvideo',
            '-pixel_format', input_pixel_format,
            '-video_size', f'{frame_width}x{frame_height}',
            '-framerate', str(self.config.framerate),
            '-i', 'pipe:0',
            '-i', str(audio_path),
            '-c:v', self.config.video_codec,
            '-pix_fmt', self.config.pixel_format,
            '-preset', self.config.video_preset,
        ]

        if self.config.video_crf is not None:
            cmd.extend(['-crf', str(self.config.video_crf)])
        elif self.config.video_bitrate:
            cmd.extend(['-b:v', self.config.video_bitrate])

        cmd.extend([
            '-c:a', self.config.audio_codec,
            '-b:a', self.config.audio_bitrate,
        ])

        cmd.extend(['-shortest', str(output_path)])
        return cmd

    @asynccontextmanager
    async def open_stream(
        self,
        job_id: str,
        frame_width: int,
        frame_height: int,
        audio_path: Path,
        output_path: Path,
        stage: Stage,
        cancel_event: asyncio.Event,
        alpha: bool = False,
    ) -> AsyncIterator[FrameSink]:
        """Start ffmpeg and yield a FrameSink for streaming frames.

        Constant memory: only one frame buffered at a time.

        Usage:
            async with encoder.open_stream(...) as sink:
                for frame in frames:
                    await sink.write(frame)
            # ffmpeg finishes and output is verified on exit

        Args:
            job_id: Job ID for logging
            frame_width: Frame width in pixels
            frame_height: Frame height in pixels
            audio_path: Path to audio file
            output_path: Path for output video
            stage: Stage with logs directory
            cancel_event: Cancellation event
            alpha: RGBA (True) or RGB (False)

        Yields:
            FrameSink with write(frame) method

        Raises:
            RunnerFailed: If ffmpeg fails
            FileNotFoundError: If audio_path missing
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        channels = 4 if alpha else 3
        expected_frame_size = frame_width * frame_height * channels
        input_pixel_format = 'rgba' if alpha else 'rgb24'
        log_path = stage.logs / "ffmpeg_encode.txt"

        cmd = self._build_cmd(frame_width, frame_height, audio_path, output_path, alpha)

        self.logger.info(
            "[FFmpegEncoder] job_id=%s: Starting streaming encode (%dx%d, %s, %s/%s, fps=%d) "
            "audio=%s → %s, log=%s",
            job_id, frame_width, frame_height, input_pixel_format,
            self.config.video_codec, self.config.video_preset, self.config.framerate,
            audio_path.name, output_path.name, log_path
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(stage.root),
        )

        self.logger.info(
            "[FFmpegEncoder] job_id=%s: ffmpeg started pid=%d, frame_size=%d bytes",
            job_id, proc.pid, expected_frame_size
        )

        log_file = log_path.open("ab")
        sink = FrameSink(proc, expected_frame_size, job_id, self.logger, cancel_event)

        # Background task: read ffmpeg stdout/stderr to log file (prevents pipe deadlock)
        async def _drain_output():
            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    log_file.write(line)
                    log_file.flush()
            except Exception:
                pass

        # Background task: watch cancel event
        async def _watch_cancel():
            try:
                await cancel_event.wait()
                if proc.returncode is None:
                    self.logger.info("[FFmpegEncoder] job_id=%s: Cancelling ffmpeg (SIGTERM)", job_id)
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        return
                    await asyncio.sleep(5.0)
                    if proc.returncode is None:
                        self.logger.warning("[FFmpegEncoder] job_id=%s: Force killing ffmpeg (SIGKILL)", job_id)
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
            except asyncio.CancelledError:
                pass

        drain_task = asyncio.create_task(_drain_output())
        cancel_task = asyncio.create_task(_watch_cancel())

        body_exc: BaseException | None = None
        try:
            yield sink
        except BaseException as e:
            body_exc = e
        finally:
            # Close stdin to signal EOF → ffmpeg finishes muxing
            if proc.stdin and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            rc = await proc.wait()
            self.logger.debug(
                "[FFmpegEncoder] job_id=%s: ffmpeg exited rc=%d, frames_written=%d",
                job_id, rc, sink.frame_count
            )
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            await asyncio.gather(drain_task, return_exceptions=True)
            log_file.close()

        def _cleanup_partial():
            if output_path.exists():
                try:
                    output_path.unlink()
                except Exception as ue:
                    self.logger.warning(
                        "[FFmpegEncoder] job_id=%s: Failed to remove partial output: %s", job_id, ue
                    )

        # Exception precedence:
        # 1. Body raised CancelledError → re-raise
        # 2. Body raised other error → surface ffmpeg failure if rc!=0, else re-raise
        # 3. cancel_event set → cancel wins even if ffmpeg exited 0 (truncated output)
        # 4. ffmpeg failed (rc!=0) → RunnerFailed with log tail
        # 5. Success

        if isinstance(body_exc, asyncio.CancelledError):
            self.logger.warning("[FFmpegEncoder] job_id=%s: Encoding cancelled", job_id)
            _cleanup_partial()
            raise body_exc

        if body_exc is not None:
            if rc != 0:
                try:
                    log_tail = log_path.read_text().splitlines()[-20:]
                    error_context = "\n".join(log_tail)
                except Exception:
                    error_context = "(could not read log)"
                raise RunnerFailed(
                    f"ffmpeg encoding failed with exit code {rc}. "
                    f"See {log_path} for details.\n\n"
                    f"Last 20 lines:\n{error_context}"
                ) from body_exc
            # ffmpeg exited 0 but body got a pipe error — this happens with -shortest
            # when audio is shorter than video stream. Output is valid if file exists.
            if isinstance(body_exc, (BrokenPipeError, ConnectionResetError)):
                self.logger.info(
                    "[FFmpegEncoder] job_id=%s: Pipe closed after ffmpeg finished (rc=0, -shortest), "
                    "frames_written=%d",
                    job_id, sink.frame_count
                )
            else:
                raise body_exc

        if cancel_event.is_set():
            self.logger.warning("[FFmpegEncoder] job_id=%s: Encoding cancelled", job_id)
            _cleanup_partial()
            raise asyncio.CancelledError(
                f"ffmpeg cancelled rc={rc}, frames_written={sink.frame_count}"
            )

        if rc != 0:
            try:
                log_tail = log_path.read_text().splitlines()[-20:]
                error_context = "\n".join(log_tail)
            except Exception:
                error_context = "(could not read log)"
            raise RunnerFailed(
                f"ffmpeg encoding failed with exit code {rc}. "
                f"See {log_path} for details.\n\n"
                f"Last 20 lines:\n{error_context}"
            )

        if not output_path.exists():
            raise RunnerFailed(f"ffmpeg completed but output file not found: {output_path}")

        output_size_mb = output_path.stat().st_size / (1024 * 1024)
        self.logger.info(
            "[FFmpegEncoder] job_id=%s: Streaming encode complete - "
            "output=%s, size=%.2f MB, frames=%d, input=%.2f MB",
            job_id, output_path.name, output_size_mb,
            sink.frame_count, sink.bytes_written / (1024 * 1024)
        )


