"""Progress tracking for render streams."""

import time
from typing import Optional

from google.protobuf.timestamp_pb2 import Timestamp
from proto.meta_pb2 import RenderMetadata
from proto.status_pb2 import Status


class ProgressTracker:
    """Tracks rendering progress and emits periodic status updates.

    Supports both online (streaming) and offline (batch) modes.
    """

    def __init__(
        self,
        emit_interval: int = 25,
        is_online: bool = False,
        total_frames: int = 0,
    ):
        """Create and initialize tracker - ready to use immediately.

        Args:
            emit_interval: Emit status every N frames (25 = ~1s at 25fps)
            is_online: True for streaming mode, False for batch mode
            total_frames: Expected total frames (offline mode only)
        """
        self.emit_interval = emit_interval
        self.is_online = is_online
        self.total_frames = total_frames if not is_online else 0
        self.frames_completed = 0
        self.start_time = time.time()

    def track_frame_progress(
        self, realtime_metric: Optional[float] = None
    ) -> Optional[RenderMetadata]:
        """Called after EVERY frame. Returns metadata when ready.

        Args:
            realtime_metric: Latency metric for online mode (negative=ahead, positive=behind)

        Returns:
            RenderMetadata if emit_interval reached, None otherwise
        """
        self.frames_completed += 1

        if self.frames_completed % self.emit_interval == 0:
            return self._create_metadata(realtime_metric)
        return None

    def _create_metadata(
        self, realtime_metric: Optional[float] = None
    ) -> RenderMetadata:
        """Create RenderMetadata with mode-appropriate fields."""
        now = time.time()

        status = Status()
        status.frames_completed = self.frames_completed

        # Set timestamps
        status.start_time.CopyFrom(self._to_timestamp(self.start_time))
        status.last_update_time.CopyFrom(self._to_timestamp(now))

        if self.is_online:
            # Online mode: include realtime_metric
            if realtime_metric is not None:
                status.realtime_metric = realtime_metric
        else:
            # Offline mode: include progress_percent and frames_total
            if self.total_frames > 0:
                status.progress_percent = self._calculate_progress()
                status.frames_total = self.total_frames

        return RenderMetadata(status=status)

    def _calculate_progress(self) -> int:
        """Compute percentage (0-100) from frames_completed / total_frames."""
        if self.total_frames <= 0:
            return 0
        return min(100, int((self.frames_completed / self.total_frames) * 100))

    @staticmethod
    def _to_timestamp(epoch_seconds: float) -> Timestamp:
        """Convert epoch seconds to protobuf Timestamp."""
        ts = Timestamp()
        ts.FromSeconds(int(epoch_seconds))
        return ts
