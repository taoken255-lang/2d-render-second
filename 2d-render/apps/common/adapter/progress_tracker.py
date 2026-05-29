"""
Reusable progress tracker for multi-stage neural network pipelines.

Design principles:
- Dynamic progress calculation based on workload (number of loops, iterations, etc.)
- Automatic logging with timestamps for observability
- Watchdog integration (keeps watchdog alive with progress updates)
- Clean API for nested pipelines
- No external dependencies beyond Python stdlib + logging

Usage:
    # Create tracker with watchdog-compatible callback
    tracker = ProgressTracker(
        callback=handle.mark_progress,  # Watchdog integration
        job_id="abc123",
        logger=my_logger
    )

    # Define stages with fixed ranges
    tracker.set_stage("preprocessing", start_pct=0, end_pct=10)
    tracker.set_stage("multitalk", start_pct=10, end_pct=20)

    # Define stage with dynamic sub-iterations (e.g., diffusion loops)
    # Calculate num_loops based on audio length
    num_loops = calculate_diffusion_loops(audio_length, fps, frame_window_size)
    tracker.set_dynamic_stage("diffusion", start_pct=20, end_pct=90, num_iterations=num_loops)

    tracker.set_stage("postprocessing", start_pct=90, end_pct=100)

    # Update within stages
    tracker.update_stage_progress("preprocessing", fraction=0.5)  # 5%
    tracker.update_stage_progress("multitalk", fraction=1.0)  # 20%

    # Update within specific iteration of dynamic stage
    for loop_idx in range(num_loops):
        # Sampling phase (60% of loop)
        for step in range(total_steps):
            tracker.update_iteration_progress(
                "diffusion",
                iteration=loop_idx,
                sub_stage="sampling",
                fraction=step / total_steps,
                weight=0.6
            )

        # VAE decode phase (40% of loop)
        for frame in range(total_frames):
            tracker.update_iteration_progress(
                "diffusion",
                iteration=loop_idx,
                sub_stage="vae",
                fraction=frame / total_frames,
                weight=0.4
            )
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class ProgressStage:
    """Represents a stage in the progress pipeline."""
    name: str
    start_pct: int
    end_pct: int
    num_iterations: int = 1  # 1 for simple stages, >1 for looped stages

    @property
    def range_pct(self) -> int:
        """Total progress range for this stage."""
        return self.end_pct - self.start_pct

    def get_iteration_range(self, iteration: int) -> tuple[int, int]:
        """
        Get progress range for a specific iteration.

        Args:
            iteration: Iteration index (0-based)

        Returns:
            (start_pct, end_pct) for this iteration
        """
        if self.num_iterations <= 1:
            return (self.start_pct, self.end_pct)

        pct_per_iteration = self.range_pct / self.num_iterations
        iter_start = self.start_pct + (iteration * pct_per_iteration)
        iter_end = iter_start + pct_per_iteration
        return (int(iter_start), int(iter_end))


class ProgressTracker:
    """
    Reusable progress tracker for multi-stage neural network pipelines.

    Features:
    - Dynamic progress range allocation based on actual workload
    - Automatic logging with timestamps
    - Watchdog integration (keeps watchdog alive)
    - Clean API for nested pipelines (preprocessing → multitalk → diffusion loops → post)

    Example:
        tracker = ProgressTracker(callback=handle.mark_progress, job_id="abc123")
        tracker.set_stage("preprocessing", 0, 10)
        tracker.set_dynamic_stage("diffusion", 20, 90, num_iterations=5)

        # Simple stage update
        tracker.update_stage_progress("preprocessing", 0.5)  # 5%

        # Iteration update with sub-stages
        tracker.update_iteration_progress("diffusion", iteration=0,
                                          sub_stage="sampling", fraction=0.8, weight=0.6)
    """

    def __init__(
        self,
        callback: Callable[[int], None],
        job_id: str = "",
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize progress tracker.

        Args:
            callback: Progress callback (e.g., handle.mark_progress) - integrates with watchdog
            job_id: Job identifier for logging
            logger: Optional logger instance (creates one if not provided)
        """
        self.callback = callback
        self.job_id = job_id
        self.logger = logger or logging.getLogger(__name__)

        self.stages: Dict[str, ProgressStage] = {}
        self.current_pct: int = 0
        self.start_time: float = time.time()
        self._last_operation: str = "init"

        # Track last progress per iteration for sub-stage calculations
        # Format: {stage_name: {iteration: {sub_stage: progress_pct}}}
        self._iteration_state: Dict[str, Dict[int, Dict[str, int]]] = {}

    def set_stage(self, name: str, start_pct: int, end_pct: int) -> None:
        """
        Define a simple stage with fixed progress range.

        Args:
            name: Stage identifier
            start_pct: Starting progress percentage (0-100)
            end_pct: Ending progress percentage (0-100)
        """
        self.stages[name] = ProgressStage(name, start_pct, end_pct, num_iterations=1)
        self.logger.debug(
            "[ProgressTracker] Stage defined: %s (%d%% → %d%%)",
            name, start_pct, end_pct
        )

    def set_dynamic_stage(
        self,
        name: str,
        start_pct: int,
        end_pct: int,
        num_iterations: int
    ) -> None:
        """
        Define a stage with dynamic sub-iterations (e.g., diffusion loops).

        Args:
            name: Stage identifier
            start_pct: Starting progress percentage (0-100)
            end_pct: Ending progress percentage (0-100)
            num_iterations: Number of iterations/loops in this stage
        """
        self.stages[name] = ProgressStage(name, start_pct, end_pct, num_iterations)
        self._iteration_state[name] = {}

        pct_per_iteration = (end_pct - start_pct) / num_iterations
        self.logger.info(
            "[ProgressTracker] Dynamic stage defined: %s (%d%% → %d%%, %d iterations, %.1f%% each) job_id=%s",
            name, start_pct, end_pct, num_iterations, pct_per_iteration, self.job_id
        )

    def update(self, pct: int, operation: Optional[str] = None) -> None:
        """
        Update progress to absolute percentage.

        Args:
            pct: Progress percentage (0-100)
            operation: Optional label describing current operation/stage
        """
        pct = max(0, min(100, pct))  # Clamp to 0-100

        if pct != self.current_pct:
            self.current_pct = pct
            elapsed = time.time() - self.start_time
            label = operation or getattr(self, "_last_operation", "unknown")
            self._last_operation = label

            # Log progress update with timing
            self.logger.info(
                "[Progress] %d%% - %s (elapsed: %.1fs) job_id=%s",
                pct, label, elapsed, self.job_id
            )

            # Call watchdog-compatible callback
            self.callback(pct)

    def update_stage_progress(self, stage_name: str, fraction: float) -> None:
        """
        Update progress within a stage (for simple, non-iterated stages).

        Args:
            stage_name: Name of the stage
            fraction: Progress within stage (0.0 = start, 1.0 = end)
        """
        if stage_name not in self.stages:
            self.logger.warning("[ProgressTracker] Unknown stage: %s", stage_name)
            return

        stage = self.stages[stage_name]
        fraction = max(0.0, min(1.0, fraction))

        pct = stage.start_pct + int(stage.range_pct * fraction)
        self.update(pct, operation=f"stage:{stage_name}")

    def update_iteration_progress(
        self,
        stage_name: str,
        iteration: int,
        sub_stage: Optional[str] = None,
        fraction: float = 0.0,
        weight: float = 1.0,
    ) -> None:
        """
        Update progress within a specific iteration of a dynamic stage.

        Supports sub-stages within iterations (e.g., sampling + VAE decode).

        Args:
            stage_name: Name of the dynamic stage
            iteration: Iteration index (0-based)
            sub_stage: Optional sub-stage name (e.g., "sampling", "vae")
            fraction: Progress within sub-stage (0.0-1.0)
            weight: Weight of this sub-stage within iteration (0.0-1.0)
                   e.g., sampling=0.6, vae=0.4 for 60%/40% split

        Example:
            # In diffusion loop 2, sampling is 80% done (sampling takes 60% of loop)
            tracker.update_iteration_progress("diffusion", iteration=2,
                                             sub_stage="sampling", fraction=0.8, weight=0.6)
        """
        if stage_name not in self.stages:
            self.logger.warning("[ProgressTracker] Unknown stage: %s", stage_name)
            return

        stage = self.stages[stage_name]

        if iteration >= stage.num_iterations:
            self.logger.warning(
                "[ProgressTracker] Iteration %d exceeds max %d for stage %s",
                iteration, stage.num_iterations, stage_name
            )
            return

        # Get progress range for this iteration
        iter_start, iter_end = stage.get_iteration_range(iteration)
        iter_range = iter_end - iter_start

        # Calculate progress within iteration
        if sub_stage:
            # Initialize iteration state if needed
            if iteration not in self._iteration_state[stage_name]:
                self._iteration_state[stage_name][iteration] = {}

            # Get baseline progress for this iteration (start of iteration)
            iter_state = self._iteration_state[stage_name][iteration]

            # Calculate absolute position within iteration for this sub-stage
            # Sub-stage contributes weight% of the iteration range
            sub_stage_range = iter_range * weight
            sub_stage_contribution = sub_stage_range * fraction

            # Find starting point: if previous sub-stages completed, start after them
            # Otherwise start at iteration start
            completed_progress = sum(iter_state.values())
            base_pct = iter_start + completed_progress

            pct = int(base_pct + sub_stage_contribution)

            # Store final progress when sub-stage completes
            if fraction >= 1.0 and sub_stage not in iter_state:
                iter_state[sub_stage] = int(sub_stage_range)
        else:
            # Simple iteration progress without sub-stages
            pct = int(iter_start + iter_range * fraction)

        label = f"{stage_name}:{sub_stage}" if sub_stage else f"{stage_name}:iteration-{iteration}"
        self.update(pct, operation=label)

    def mark_stage_complete(self, stage_name: str) -> None:
        """
        Mark a stage as complete (sets progress to stage end_pct).

        Args:
            stage_name: Name of the stage to mark complete
        """
        if stage_name not in self.stages:
            self.logger.warning("[ProgressTracker] Unknown stage: %s", stage_name)
            return

        stage = self.stages[stage_name]
        elapsed = time.time() - self.start_time

        self.logger.info(
            "[ProgressTracker] Stage complete: %s (%.1fs) job_id=%s",
            stage_name, elapsed, self.job_id
        )

        self.update(stage.end_pct, operation=f"stage_complete:{stage_name}")

    def get_eta(self) -> Optional[float]:
        """
        Calculate estimated time remaining based on current progress velocity.

        Returns:
            Estimated seconds remaining, or None if progress is 0
        """
        if self.current_pct == 0:
            return None

        elapsed = time.time() - self.start_time
        velocity = self.current_pct / elapsed  # pct per second

        if velocity <= 0:
            return None

        remaining_pct = 100 - self.current_pct
        eta = remaining_pct / velocity

        return eta


# Example usage for InfiniteTalk
def example_usage():
    """
    Example: How to use ProgressTracker in InfiniteTalk adapter.
    """
    # Mock watchdog handle
    class MockHandle:
        def mark_progress(self, pct):
            logger.info("Watchdog updated: %s%%", pct)

    handle = MockHandle()

    # Create tracker
    tracker = ProgressTracker(
        callback=handle.mark_progress,
        job_id="test-job-123",
        logger=logger
    )

    # Define pipeline stages
    tracker.set_stage("preprocessing", 0, 10)
    tracker.set_stage("multitalk", 10, 20)

    # Calculate number of diffusion loops based on audio
    audio_length = 30  # seconds
    fps = 15
    num_loops = 5  # Would be calculated dynamically

    tracker.set_dynamic_stage("diffusion", 20, 90, num_iterations=num_loops)
    tracker.set_stage("postprocessing", 90, 100)

    # Simulate preprocessing
    tracker.update_stage_progress("preprocessing", 0.5)  # 5%
    tracker.mark_stage_complete("preprocessing")  # 10%

    # Simulate multitalk
    tracker.update_stage_progress("multitalk", 0.5)  # 15%
    tracker.mark_stage_complete("multitalk")  # 20%

    # Simulate diffusion loops
    for loop_idx in range(num_loops):
        logger.info("--- Loop %s ---", loop_idx)
        # Sampling phase (60% of each loop)
        for step in range(1, 7):  # 1-6 instead of 0-5 to avoid fraction=0
            tracker.update_iteration_progress(
                "diffusion",
                iteration=loop_idx,
                sub_stage="sampling",
                fraction=step / 6,
                weight=0.6
            )

        # VAE decode phase (40% of each loop)
        for frame in range(1, 11):  # 1-10 instead of 0-9
            tracker.update_iteration_progress(
                "diffusion",
                iteration=loop_idx,
                sub_stage="vae",
                fraction=frame / 10,
                weight=0.4
            )

    # Complete diffusion
    tracker.mark_stage_complete("diffusion")  # 90%

    # Postprocessing
    tracker.update_stage_progress("postprocessing", 1.0)  # 100%

    logger.info("ETA: %s", tracker.get_eta())


def calculate_diffusion_loops(
    audio_length_sec: float,
    fps: int,
    frame_window_size: int = 81,
    overlap: int = 9
) -> int:
    """
    Calculate number of diffusion loops based on audio length.

    Based on observed log patterns:
    - Each loop processes ~81 frames (frame_window_size)
    - Loops overlap by ~9 frames
    - Effective frames per loop: 81 - 9 = 72

    Args:
        audio_length_sec: Audio duration in seconds
        fps: Frames per second
        frame_window_size: Frames processed per loop (default 81 from logs)
        overlap: Overlap between consecutive loops (default 9 from logs)

    Returns:
        Number of diffusion loops required (minimum 1)

    Examples:
        >>> calculate_diffusion_loops(3, 15)   # 45 frames
        1
        >>> calculate_diffusion_loops(30, 15)  # 450 frames
        7
        >>> calculate_diffusion_loops(300, 15) # 4500 frames
        63
    """
    import math
    total_frames = int(audio_length_sec * fps)
    effective_frames_per_loop = frame_window_size - overlap
    num_loops = math.ceil((total_frames - overlap) / effective_frames_per_loop)
    return max(1, num_loops)


if __name__ == "__main__":
    example_usage()
