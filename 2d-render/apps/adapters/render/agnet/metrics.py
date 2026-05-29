from __future__ import annotations

import logging

from prometheus_client import CollectorRegistry, Histogram

logger = logging.getLogger(__name__)

_LABEL_NAMES = ("service", "engine")
# Keep broad upper buckets because this temporary bridge metric can see
# extremely large ratios on short input audio while startup/render overheads
# stay mostly fixed. Revisit once Agnet service owns the native metric.
_RENDER_TIME_TO_AUDIO_RATIO_BUCKETS = (
    0.25,
    0.5,
    0.75,
    1,
    1.25,
    1.5,
    2,
    3,
    5,
    8,
    13,
    21,
    34,
    55,
    89,
    144,
    233,
    377,
    610,
    987,
    1597,
)


class AgnetBridgeMetrics:
    """Agnet bridge-only metrics bound to an existing app registry.

    This helper intentionally stays local to the current bridge adapter.
    The long-term owner for workload-normalized render metrics should be the
    Agnet service itself once the adapter logic is merged into the service
    container and render timings become native service facts.
    """

    def __init__(
        self,
        *,
        registry: CollectorRegistry,
        service: str,
        engine: str,
    ) -> None:
        self._labels = {"service": service, "engine": engine}
        self._render_time_to_audio_ratio = Histogram(
            "render_agnet_bridge_render_time_to_audio_ratio",
            (
                "Temporary bridge-side ratio: render pipeline wall time divided by input audio duration. "
                "Values above 1 mean slower than real-time; this should move into Agnet service-native "
                "metrics after the adapter/service merge."
            ),
            _LABEL_NAMES,
            buckets=_RENDER_TIME_TO_AUDIO_RATIO_BUCKETS,
            registry=registry,
        )

    def observe_render_time_to_audio_ratio(
        self,
        *,
        render_seconds: float,
        audio_duration_seconds: float,
    ) -> None:
        if render_seconds < 0:
            logger.warning(
                "[AgnetBridgeMetrics::observe_render_time_to_audio_ratio] Negative render duration ignored - "
                "render_seconds=%.6f",
                render_seconds,
            )
            return
        if audio_duration_seconds <= 0:
            logger.warning(
                "[AgnetBridgeMetrics::observe_render_time_to_audio_ratio] Non-positive audio duration ignored - "
                "audio_duration_seconds=%.6f",
                audio_duration_seconds,
            )
            return

        self._render_time_to_audio_ratio.labels(**self._labels).observe(render_seconds / audio_duration_seconds)
