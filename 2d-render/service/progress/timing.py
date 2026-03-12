"""Timing utilities for performance logging.

Log levels:
- INFO: Model loading (startup), timing summary (per-request) - always visible
- DEBUG: Avatar registration breakdown - visible when debugging
- TRACE: Per-frame inference measurements - only when you really need it

Usage:
    # INFO level for important ops (always visible)
    with log_timing("load_model", level="INFO"):
        model = load()

    # DEBUG level for details (default)
    with log_timing("avatar_decode"):
        decode()

    # TimingTracker for aggregated per-request timing
    tracker = TimingTracker()
    with tracker.measure("inference"):
        run()
    tracker.log_summary(label="MY SUBSYSTEM")  # logs at INFO level
"""
import time
from contextlib import contextmanager
from collections import defaultdict
from loguru import logger
from config import Config


def _format_time(ms: float) -> str:
    """Format time in seconds with 3 decimal places for consistency."""
    return f"{ms / 1000:.3f}s"


@contextmanager
def log_timing(operation: str, level: str = "DEBUG"):
    """Timing context manager with configurable log level.

    Args:
        operation: Name of the operation being timed
        level: Log level - "INFO" for startup/important ops, "DEBUG" for details

    Usage:
        with log_timing("load_model", level="INFO"):  # always visible
            ...
        with log_timing("avatar_decode"):  # DEBUG level (default)
            ...
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log_func = getattr(logger, level.lower())
        log_func(f"[TIMING] {operation}: {_format_time(elapsed_ms)}")


class TimedReader:
    """File-like wrapper that measures time spent in read operations."""

    def __init__(self, f):
        self._f = f
        self.read_ms = 0.0

    def read(self, n=-1):
        start = time.perf_counter()
        data = self._f.read(n)
        self.read_ms += (time.perf_counter() - start) * 1000
        return data

    def readinto(self, b):
        start = time.perf_counter()
        n = self._f.readinto(b)
        self.read_ms += (time.perf_counter() - start) * 1000
        return n

    def readline(self, n=-1):
        start = time.perf_counter()
        data = self._f.readline(n)
        self.read_ms += (time.perf_counter() - start) * 1000
        return data

    def __getattr__(self, name):
        return getattr(self._f, name)


class TimingTracker:
    """Aggregates timing measurements and provides summary.

    Behavior based on LOG_LEVEL:
    - INFO: disabled (zero overhead)
    - DEBUG: enabled, summary only
    - TRACE: enabled, summary + per-frame logs (for deep inspection)
    """

    def __init__(self, enabled: bool | None = None, log_each: bool | None = None):
        if enabled is None:
            enabled = Config.LOG_LEVEL in ("DEBUG", "TRACE")
        self.enabled = enabled

        if log_each is None:
            log_each = Config.LOG_LEVEL == "TRACE"
        self._log_each = bool(log_each) and self.enabled
        self._timings = defaultdict(list)
        self._t0 = time.perf_counter()
        self._points = {}
        self._last_lap = None

    def stage(self, label: str):
        """Log a stage header (for stable, greppable timing buckets)."""
        if not self.enabled:
            return
        logger.info(f"[TIMING] [{label}]")

    def delta(self, name: str, start_point: str, end_point: str):
        """Compute elapsed time between two recorded points and store as a timing op."""
        if not self.enabled:
            return
        if start_point in self._points and end_point in self._points:
            self.add_ms(name, self._points[end_point] - self._points[start_point])

    def point(self, name: str):
        """Record wall-clock checkpoint since tracker creation (TTFF, end)."""
        if not self.enabled:
            return
        self._points[name] = (time.perf_counter() - self._t0) * 1000

    def point_once(self, name: str):
        """Record checkpoint only on first call (for first-output timing in loops)."""
        if not self.enabled:
            return
        if name in self._points:
            return
        self._points[name] = (time.perf_counter() - self._t0) * 1000

    def point_ms(self, name: str, elapsed_ms: float):
        """Record checkpoint at a provided elapsed timestamp (ms since external start)."""
        if not self.enabled:
            return
        self._points[name] = elapsed_ms

    def point_once_ms(self, name: str, elapsed_ms: float):
        """Record checkpoint once using a provided elapsed timestamp (ms since external start)."""
        if not self.enabled:
            return
        if name in self._points:
            return
        self._points[name] = elapsed_ms


    def reset_clock(self, clear_points: bool = True):
        """Reset the reference clock used by point/point_once."""
        if not self.enabled:
            return
        self._t0 = time.perf_counter()
        self._last_lap = None
        if clear_points:
            self._points.clear()

    def add_ms(self, operation: str, elapsed_ms: float):
        """Add timing measurement directly (for external timing sources)."""
        if not self.enabled:
            return
        self._timings[operation].append(elapsed_ms)
        if self._log_each:
            logger.trace(f"[TIMING] {operation}: {_format_time(elapsed_ms)}")

    def call(self, operation: str, fn, *args, **kwargs):
        """Time a single blocking call (queue.get, etc)."""
        if not self.enabled:
            return fn(*args, **kwargs)
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            self.add_ms(operation, (time.perf_counter() - start) * 1000)

    def lap(self, operation: str):
        """Record time between loop iterations (gRPC pacing/backpressure proxy)."""
        if not self.enabled:
            return
        now = time.perf_counter()
        if self._last_lap is None:
            self._last_lap = now
            return
        self.add_ms(operation, (now - self._last_lap) * 1000)
        self._last_lap = now

    @contextmanager
    def measure(self, operation: str):
        """Measure and accumulate time for an operation.

        When enabled=False: immediately yields and returns (no timing, no logging).
        """
        if not self.enabled:
            # Production: true no-op, zero overhead
            yield
            return
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self._timings[operation].append(elapsed_ms)
            if self._log_each:
                logger.trace(f"[TIMING] {operation}: {_format_time(elapsed_ms)}")

    def log_summary(self, label: str = "", include_points: bool = True, extra: dict | None = None):
        """Log aggregated summary at INFO level (always visible)."""
        if not self.enabled:
            return
        prefix = f"[TIMING] [{label}]" if label else "[TIMING]"
        if include_points and self._points:
            pts = ", ".join(f"{k}={_format_time(v)}" for k, v in self._points.items())
            logger.info(f"{prefix} points: {pts}")

        extra_parts = []
        if extra:
            extra_parts = [f"{k}={v}" for k, v in extra.items()]

        if not self._timings:
            if extra_parts:
                logger.info(f"{prefix} summary: {', '.join(extra_parts)}")
            return

        parts = []
        for op, times in self._timings.items():
            op_total = sum(times)
            count = len(times)
            if count == 1:
                parts.append(f"{op}={_format_time(op_total)}")
            else:
                parts.append(f"{op}={_format_time(op_total)}({count}x)")

        parts.extend(extra_parts)
        logger.info(f"{prefix} summary: {', '.join(parts)}")

    def reset(self):
        """Clear all timings."""
        self._timings.clear()
