"""
Shared adapter watchdog for self-healing on progress stalls.

Provides:
- ProgressWatchdogConfig: Environment-driven configuration
- JobWatchdogHandle: Progress tracking and cancel state management
- run_watchdog: Dual-branch monitoring (cancel-stall + general-stall)

Design:
- Adapters own their lifecycle via self-monitoring watchdog
- Hard-kill (os._exit(137)) triggers container restart for recovery
- Encapsulates stall detection logic - adapters just wire progress callbacks

References:
- SRE book: Monitoring distributed systems (self-healing pattern)
- Azure health monitoring: https://learn.microsoft.com/en-us/azure/architecture/patterns/health-endpoint-monitoring
- 12-Factor disposability: https://12factor.net/disposability
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, NoReturn, Optional

from apps.common.helpers import env_float, env_int


logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
@dataclass
class ProgressWatchdogConfig:
    """
    Watchdog configuration loaded from environment variables.

    Attributes:
        cancel_grace_sec: Timeout after cancel request before hard-kill (default: 60s)
        stall_minutes: General progress stall timeout before hard-kill (default: 27 min, 0 to disable)
        tick_interval_sec: How often watchdog wakes to check conditions (default: 30s)
    """
    cancel_grace_sec: float
    stall_minutes: float
    tick_interval_sec: float

    @classmethod
    def from_env(cls) -> ProgressWatchdogConfig:
        """
        Load watchdog config from environment variables.

        Environment variables:
        - CANCEL_GRACE_SEC: Timeout for cancel-stall detection (default: 60)
        - ADAPTER_PROGRESS_STALL_MINUTES: Timeout for general stall (default: 27, 0 disables)
        - WATCHDOG_TICK_SEC: Watchdog poll interval (default: 1 for fast cancel sync)

        Returns:
            ProgressWatchdogConfig with env-driven or default values
        """
        return cls(
            cancel_grace_sec=env_float("CANCEL_GRACE_SEC", 60.0),
            stall_minutes=env_float("ADAPTER_PROGRESS_STALL_MINUTES", 27.0),
            tick_interval_sec=env_float("WATCHDOG_TICK_SEC", 1.0),
        )

    @property
    def stall_enabled(self) -> bool:
        """Check if general stall monitoring is enabled (stall_minutes > 0)."""
        return self.stall_minutes > 0.0

    @property
    def stall_limit_sec(self) -> float:
        """Convert stall_minutes to seconds for internal calculations."""
        return max(self.stall_minutes, 0.0) * 60.0


# -----------------------------------------------------------------------------
# JobHandle (Progress tracking + cancel state)
# -----------------------------------------------------------------------------
@dataclass
class JobWatchdogHandle:
    """
    Per-job state for watchdog monitoring.

    Tracks:
    - Cancel state (asyncio.Event + threading.Event for cross-thread signaling)
    - Progress timestamps and percentages
    - Watchdog task lifecycle

    Bridge pattern: Maintains both asyncio.Event (for watchdog coroutine) and
    threading.Event (for engine worker thread), set atomically on cancel.
    """
    # Cancel events (dual bridge for asyncio + threading)
    async_evt: asyncio.Event
    thread_evt: threading.Event = field(default_factory=threading.Event)

    # Progress tracking
    last_progress_pct: Optional[int] = None
    last_progress_ts: float = field(default_factory=time.monotonic)

    # Cancel tracking
    cancel_set_ts: Optional[float] = None

    # Watchdog task (managed by runner)
    watchdog_task: Optional[asyncio.Task] = None

    def mark_progress(self, pct: Optional[int]) -> None:
        """
        Update progress state (called from worker thread via progress callback).

        Args:
            pct: Progress percentage (0-100) or None if indeterminate
        """
        self.last_progress_pct = pct
        self.last_progress_ts = time.monotonic()

    def note_cancel_sync(self) -> None:
        """
        Record cancel request timestamp (called when cancel signal is set).

        Should be called atomically with setting both async_evt and thread_evt.
        """
        if self.cancel_set_ts is None:
            self.cancel_set_ts = time.monotonic()


# -----------------------------------------------------------------------------
# Watchdog coroutine (Dual-branch monitoring)
# -----------------------------------------------------------------------------
async def run_watchdog(
    handle: JobWatchdogHandle,
    config: ProgressWatchdogConfig,
    job_id: str,
    *,
    hard_kill: Callable[[str], NoReturn],
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Monitor job for stall conditions and trigger hard-kill on timeout.

    Dual-branch logic:
    1. Cancel-stall: Cancel requested but no progress for cancel_grace_sec
    2. General-stall: No progress for stall_minutes (disabled if stall_minutes <= 0)

    When either condition triggers, calls hard_kill(reason) which should exit
    the process (e.g., os._exit(137)) for container restart.

    Args:
        handle: Job state tracker
        config: Watchdog configuration
        job_id: Job identifier for logging
        hard_kill: Callback to terminate process (must not return)
        logger: Optional logger (uses module logger if not provided)

    Design notes:
    - Wakes every config.tick_interval_sec to check conditions
    - Syncs cancel signal from asyncio → threading.Event
    - Logs structured data for monitoring/alerting
    - Exit code 137 (128 + SIGKILL) recognized by Docker/k8s
    """
    # Use provided logger or fall back to module logger
    log = logger or logging.getLogger(__name__)

    log.info(
        "Watchdog started: job_id=%s cancel_grace=%.0fs stall_threshold=%.1fmin tick=%.0fs",
        job_id,
        config.cancel_grace_sec,
        config.stall_minutes,
        config.tick_interval_sec,
    )

    while True:
        await asyncio.sleep(config.tick_interval_sec)

        # Sync cancel signal: asyncio.Event → threading.Event (bridge pattern)
        if handle.async_evt.is_set() and not handle.thread_evt.is_set():
            handle.thread_evt.set()
            handle.note_cancel_sync()
            log.info("Watchdog: synced cancel signal to worker thread job_id=%s", job_id)

        now = time.monotonic()

        # Branch A: Cancel-stall detection (existing logic)
        # Fires when cancel requested but progress stalled for cancel_grace_sec
        if handle.async_evt.is_set():
            since_cancel = now - (handle.cancel_set_ts or now)
            since_progress = now - handle.last_progress_ts

            if since_cancel >= config.cancel_grace_sec and since_progress >= config.cancel_grace_sec:
                stall_min = since_progress / 60.0
                log.error(
                    "Adapter self-kill: cancel stalled (no progress after cancel)",
                    extra={
                        "job_id": job_id,
                        "last_progress_pct": handle.last_progress_pct,
                        "stall_duration_min": round(stall_min, 1),
                        "since_cancel_sec": round(since_cancel, 1),
                        "threshold_sec": config.cancel_grace_sec,
                        "exit_code": 137,
                        "reason": "cancel_stall",
                    },
                )
                hard_kill("cancel_stall")

        # Branch B: General-stall detection (new logic)
        # Fires when no progress for stall_minutes, regardless of cancel state
        if config.stall_enabled:
            since_progress = now - handle.last_progress_ts

            if since_progress >= config.stall_limit_sec:
                stall_min = since_progress / 60.0
                log.error(
                    "Adapter self-kill: progress stalled (no progress for threshold)",
                    extra={
                        "job_id": job_id,
                        "last_progress_pct": handle.last_progress_pct,
                        "stall_duration_min": round(stall_min, 1),
                        "threshold_min": config.stall_minutes,
                        "exit_code": 137,
                        "reason": "progress_stall",
                    },
                )
                hard_kill("progress_stall")
