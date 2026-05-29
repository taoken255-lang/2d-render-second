from __future__ import annotations

import time
from collections.abc import Iterable, Sequence

from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest

HTTP_REQUEST_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 80)
_DEFAULT_SKIP_PATHS = frozenset(("/metrics", "/healthz", "/readyz"))
_REQUEST_LABEL_NAMES = ("service", "method", "route", "status_code")


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else "_unmatched"


def install_http_metrics(
    app: FastAPI,
    *,
    service: str,
    registry: CollectorRegistry | None = None,
    skip_paths: Iterable[str] = _DEFAULT_SKIP_PATHS,
    buckets: Sequence[float] = HTTP_REQUEST_DURATION_BUCKETS,
) -> CollectorRegistry:
    """Install low-cardinality HTTP request metrics on a FastAPI app."""

    if getattr(app.state, "_render_http_metrics_installed", False):
        raise RuntimeError("HTTP metrics already installed on this FastAPI app")

    if any(getattr(route, "path", None) == "/metrics" for route in app.router.routes):
        raise RuntimeError("Cannot install HTTP metrics: /metrics route already exists")

    app.state._render_http_metrics_installed = True
    metric_registry = registry or CollectorRegistry(auto_describe=True)
    skip_paths_set = frozenset(skip_paths)

    request_total = Counter(
        "render_http_requests",
        "Total number of HTTP requests handled by this service.",
        _REQUEST_LABEL_NAMES,
        registry=metric_registry,
    )
    request_duration = Histogram(
        "render_http_request_duration_seconds",
        "HTTP request latency distribution for this service.",
        _REQUEST_LABEL_NAMES,
        buckets=tuple(buckets),
        registry=metric_registry,
    )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=generate_latest(metric_registry), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def _instrument_http_requests(request: Request, call_next):
        if request.url.path in skip_paths_set:
            return await call_next(request)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - start
            labels = {
                "service": service,
                "method": request.method,
                "route": _route_label(request),
                "status_code": "500",
            }
            request_total.labels(**labels).inc()
            request_duration.labels(**labels).observe(duration)
            raise

        duration = time.perf_counter() - start
        labels = {
            "service": service,
            "method": request.method,
            "route": _route_label(request),
            "status_code": str(response.status_code),
        }
        request_total.labels(**labels).inc()
        request_duration.labels(**labels).observe(duration)
        return response

    return metric_registry
