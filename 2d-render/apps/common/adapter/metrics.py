from __future__ import annotations

import logging
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

_BASE_LABEL_NAMES = ("service", "engine")
_OUTCOME_LABEL_NAMES = ("service", "engine", "outcome")
_JOB_DURATION_BUCKETS = (1, 2.5, 5, 10, 20, 30, 45, 60, 90, 120, 180, 300, 600, 900, 1800, 3600)
_RECENT_FINISHED_LIMIT = 4096


class AdapterJobMetrics:
    """Passive adapter job lifecycle metrics bound to an existing app registry."""

    def __init__(
        self,
        *,
        registry: CollectorRegistry,
        service: str,
        engine: str,
        recent_finished_limit: int = _RECENT_FINISHED_LIMIT,
    ) -> None:
        self._labels = {"service": service, "engine": engine}
        self._executing_job_ids: set[str] = set()
        self._recent_finished_job_ids: dict[str, None] = {}
        self._recent_finished_limit = max(int(recent_finished_limit), 1)
        self._execution_started_monotonic: dict[str, float] = {}
        self._jobs_in_progress = Gauge(
            "render_adapter_jobs_in_progress",
            "Current number of adapter jobs actively executing in the adapter.",
            _BASE_LABEL_NAMES,
            registry=registry,
        )
        self._jobs_finished = Counter(
            "render_adapter_jobs_finished",
            "Total number of adapter jobs finished by final outcome.",
            _OUTCOME_LABEL_NAMES,
            registry=registry,
        )
        self._job_execution = Histogram(
            "render_adapter_job_execution_seconds",
            "Adapter-side job execution time from actual execution start to terminal outcome.",
            _OUTCOME_LABEL_NAMES,
            buckets=_JOB_DURATION_BUCKETS,
            registry=registry,
        )

    def _outcome_labels(self, outcome: str) -> dict[str, str]:
        labels = dict(self._labels)
        labels["outcome"] = outcome
        return labels

    def _remember_finished(self, job_id: str) -> None:
        self._recent_finished_job_ids.pop(job_id, None)
        self._recent_finished_job_ids[job_id] = None
        while len(self._recent_finished_job_ids) > self._recent_finished_limit:
            oldest_job_id = next(iter(self._recent_finished_job_ids))
            self._recent_finished_job_ids.pop(oldest_job_id)

    def on_job_execution_started(self, job_id: str, *, started_monotonic: float | None = None) -> None:
        if job_id in self._executing_job_ids or job_id in self._recent_finished_job_ids:
            logger.warning(
                "[AdapterJobMetrics::on_job_execution_started] Duplicate execution start ignored - job_id=%s",
                job_id,
            )
            return
        self._executing_job_ids.add(job_id)
        self._execution_started_monotonic[job_id] = (
            started_monotonic if started_monotonic is not None else time.monotonic()
        )
        self._jobs_in_progress.labels(**self._labels).inc()

    def on_job_finished(
        self,
        job_id: str,
        outcome: str,
    ) -> None:
        if job_id in self._recent_finished_job_ids:
            logger.warning(
                "[AdapterJobMetrics::on_job_finished] Duplicate finish ignored - job_id=%s outcome=%s",
                job_id,
                outcome,
            )
            return

        if job_id in self._executing_job_ids:
            self._executing_job_ids.remove(job_id)
            self._jobs_in_progress.labels(**self._labels).dec()
        else:
            logger.warning(
                "[AdapterJobMetrics::on_job_finished] Finish requested without active execution - job_id=%s outcome=%s",
                job_id,
                outcome,
            )

        self._remember_finished(job_id)
        labels = self._outcome_labels(outcome)
        self._jobs_finished.labels(**labels).inc()

        execution_seconds = self._execution_seconds(job_id)
        if execution_seconds is not None:
            self._job_execution.labels(**labels).observe(execution_seconds)

    def _execution_seconds(self, job_id: str) -> float | None:
        started_monotonic = self._execution_started_monotonic.pop(job_id, None)
        if started_monotonic is None:
            return None
        delta = time.monotonic() - started_monotonic
        if delta < 0:
            logger.warning(
                "[AdapterJobMetrics::_execution_seconds] Negative duration ignored - job_id=%s started_monotonic=%.6f",
                job_id,
                started_monotonic,
            )
            return None
        return delta
