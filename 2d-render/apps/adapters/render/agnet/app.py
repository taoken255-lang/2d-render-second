"""FastAPI application for Agnet adapter - Bridge to Agnet gRPC service.

Simplified adapter with no parameter profiles (bridge pattern - parameters passed through to gRPC).
Implements universal adapter HTTP contract compatible with async-2d-render worker.

Reference: apps/finik_adapter/app.py (but simplified for bridge pattern)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse

from apps.common.adapter.base_models import StartRequest, StartResponse, PollResponse, JobRec
from apps.common.adapter.metrics import AdapterJobMetrics
from apps.common.adapter.retention import apply_stage_retention
from apps.common.adapter.test_helper import run_self_test as run_adapter_self_test
from apps.common.adapter.error_classifier import classify_engine_error
from apps.common.adapter.io_helpers import materialize_avatar_assets
from apps.common.logging_config import setup_logging
from apps.common.helpers import env_float, env_bool, env_int, env_str
from apps.common.metrics import install_http_metrics
from apps.adapters.render.agnet.runner import Runner, RunnerCfg, RunnerFailed
from apps.adapters.render.agnet.metrics import AgnetBridgeMetrics
from apps.common.adapter.stage import Stage


setup_logging()
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
TICK_SEC = env_float("ADAPTER_TICK_SEC", 1.1)
HTTP_TIMEOUT = env_float("WORKER_HTTP_TIMEOUT_SEC", 20.0)
LOG_SENSITIVE = env_bool("LOG_SENSITIVE", False)
ADAPTER_TEST_MODE = env_bool("ADAPTER_TEST_MODE", False)
ADAPTER_JOB_TIMEOUT_SEC = env_int("ADAPTER_JOB_TIMEOUT_SEC", 24 * 3600)
ADAPTER_JOB_PATH = Path(env_str("ADAPTER_JOB_PATH", "/work") or "/work")
ADAPTER_KEEP_LAST_RESULTS = env_int("ADAPTER_KEEP_LAST_RESULTS", 10)

# Directories are created lazily by Stage.create() with parents=True, exist_ok=True
# No need to pre-create at module import time (fails with volume mounts)

runner_cfg = RunnerCfg(
    work_root=ADAPTER_JOB_PATH,
    http_timeout=HTTP_TIMEOUT,
    job_timeout_sec=float(ADAPTER_JOB_TIMEOUT_SEC) if ADAPTER_JOB_TIMEOUT_SEC else None,
)

# Runner is initialized by server.py via init_runner() after StreamingService is ready
runner: Runner = None


def init_runner(streaming_service) -> None:
    """Injected by server.py after StreamingService is created, before uvicorn starts."""
    global runner
    runner = Runner(runner_cfg, logger, streaming_service=streaming_service)


def _data_plane_base_url_from_input_url(input_url: str) -> Optional[str]:
    parsed = urlparse(input_url or "")
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


# -----------------------------------------------------------------------------
# Job registry
# -----------------------------------------------------------------------------
class Jobs:
    """Job registry with concurrency control.

    Semaphore is set to 1 — adapter runs in-process on a single GPU,
    so jobs must be serialized.
    """

    def __init__(self) -> None:
        self._by_job: Dict[str, JobRec] = {}
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(1)  # Single GPU: one job at a time
        self._active = 0

    async def get_by_job(self, job_id: str) -> Optional[JobRec]:
        return self._by_job.get(job_id)

    async def start_or_get(self, req: StartRequest) -> JobRec:
        async with self._lock:
            existing = self._by_job.get(req.job_id)
            if existing:
                logger.info(
                    "adapter: duplicate start for job_id=%s; returning existing state=%s",
                    req.job_id, existing.state
                )
                return existing

            outputs = {
                "video_key": req.outputs.video_key,
            }

            rec = JobRec(
                job_id=req.job_id,
                state="running",
                progress=0,
                outputs=outputs,
                params={},  # No params for Agnet (bridge pattern)
                started_at=time.time(),
            )
            self._by_job[req.job_id] = rec
            rec.task = asyncio.create_task(self._run(req, rec))
            logger.info("adapter: job started job_id=%s", req.job_id)
            return rec

    async def request_cancel(self, job_id: str) -> JobRec:
        async with self._lock:
            rec = self._by_job.get(job_id)
            if not rec:
                raise HTTPException(status_code=404, detail="Unknown job_id")
            if rec.state in ("done", "failed"):
                return rec
            if not rec.cancel_requested:
                rec.cancel_requested = True
                rec.cancel_event.set()
            return rec

    async def _run(self, req: StartRequest, rec: JobRec) -> None:
        render_seconds: float | None = None
        input_audio_duration_seconds: float | None = None

        async def _check_cancel(stage: str) -> None:
            if rec.cancel_event.is_set():
                logger.info(
                    "adapter: cancel acknowledged during %s job_id=%s",
                    stage, req.job_id
                )
                raise asyncio.CancelledError()

        async def _await_with_cancel(coro, stage: str):
            await _check_cancel(f"before {stage}")
            result = await coro
            await _check_cancel(f"after {stage}")
            return result

        if ADAPTER_TEST_MODE:
            ADAPTER_JOB_METRICS.on_job_execution_started(req.job_id)
            await run_adapter_self_test(
                req=req,
                rec=rec,
                tick_sec=TICK_SEC,
                http_timeout=HTTP_TIMEOUT,
                work_root=ADAPTER_JOB_PATH,
                logger=logger,
            )
            rec.state = "done"
            rec.progress = 100
            rec.ended_at = time.time()
            ADAPTER_JOB_METRICS.on_job_finished(req.job_id, rec.state)
            return

        await self._sem.acquire()
        self._active += 1
        ADAPTER_JOB_METRICS.on_job_execution_started(req.job_id)
        stage: Optional[Stage] = None
        try:
            stage = Stage.create(runner_cfg.work_root, req.job_id)

            await _await_with_cancel(
                runner.validate_inputs(
                    req.inputs.audio_url,
                    req.inputs.photo_url,
                    transfer_auth=req.transfer_auth,
                ),
                "validate_inputs"
            )
            paths = await _await_with_cancel(
                runner.fetch_inputs(
                    stage,
                    req.inputs.audio_url,
                    req.inputs.photo_url,
                    audio_filename=req.inputs.audio_filename,
                    photo_filename=req.inputs.photo_filename,
                    transfer_auth=req.transfer_auth,
                ),
                "fetch_inputs"
            )
            prefetched_avatar = await _await_with_cancel(
                materialize_avatar_assets(
                    req.params,
                    data_plane_base_url=_data_plane_base_url_from_input_url(req.inputs.audio_url),
                    timeout=runner_cfg.http_timeout,
                    transfer_auth=req.transfer_auth,
                    logger=logger,
                ),
                "materialize_avatar_assets",
            )
            logger.info(
                "[AVATAR VERSION] app._run prefetched version=%s name=%s path=%s",
                prefetched_avatar.version if prefetched_avatar is not None and prefetched_avatar.version is not None else "-",
                prefetched_avatar.name if prefetched_avatar is not None else "-",
                prefetched_avatar.path if prefetched_avatar is not None else "-",
            )

            rec.progress = max(rec.progress, 15)

            def _on_progress(pct: int) -> None:
                # Clamp to 99% max - only set 100% when state=done
                value = max(rec.progress, min(int(pct), 99))
                if value > rec.progress:
                    rec.progress = value

            async def _invoke():
                return await runner.run_inference(
                    stage,
                    audio_path=paths["audio"],
                    photo_path=paths["photo"],
                    render_params=req.params,  # Pass params (may contain avatar_id)
                    on_progress=_on_progress,
                    cancel_event=rec.cancel_event,
                    job_id=req.job_id,
                    prefetched_avatar=prefetched_avatar,
                )

            if runner_cfg.job_timeout_sec and runner_cfg.job_timeout_sec > 0:
                inference_result = await asyncio.wait_for(_invoke(), timeout=runner_cfg.job_timeout_sec)
            else:
                inference_result = await _invoke()

            out_path = inference_result.output_path
            render_seconds = inference_result.render_seconds
            input_audio_duration_seconds = inference_result.input_audio_duration_seconds

            rec.progress = max(rec.progress, 85)
            await _check_cancel("after run_inference")

            await _await_with_cancel(
                runner.upload_output(
                    stage,
                    req.outputs.video_upload_url,
                    req.outputs.content_type or "video/mp4",
                    Path(out_path),
                    req.transfer_auth,
                ),
                "upload_output",
            )

            rec.progress = 100
            rec.state = "done"
            rec.ended_at = time.time()
            rec.outputs["video_path"] = str(out_path)
            if render_seconds is None or input_audio_duration_seconds is None:
                logger.warning(
                    "adapter: agnet bridge metric skipped job_id=%s render_seconds=%r audio_duration_seconds=%r",
                    req.job_id,
                    render_seconds,
                    input_audio_duration_seconds,
                )
            else:
                try:
                    AGNET_BRIDGE_METRICS.observe_render_time_to_audio_ratio(
                        render_seconds=render_seconds,
                        audio_duration_seconds=input_audio_duration_seconds,
                    )
                except Exception:
                    logger.exception("adapter: agnet bridge metric emission failed job_id=%s", req.job_id)

        except asyncio.CancelledError:
            rec.state = "cancelled"
            rec.error = {"code": "CANCELLED", "message": "adapter cancel request"}
            rec.ended_at = time.time()
            logger.warning("adapter: job cancelled job_id=%s", req.job_id)
        except asyncio.TimeoutError:
            rec.state = "failed"
            rec.error = {"code": "DEADLINE_EXCEEDED", "message": "job exceeded time limit"}
            rec.ended_at = time.time()
            logger.error("adapter: job timeout job_id=%s", req.job_id)
        except RunnerFailed as exc:
            code = classify_engine_error(str(exc), "")
            rec.state = "failed"
            rec.error = {"code": code, "message": str(exc)}
            rec.ended_at = time.time()
            logger.error("adapter: job failed job_id=%s code=%s error=%s", req.job_id, code, exc)
        except Exception as exc:
            code = classify_engine_error(str(exc), "")
            rec.state = "failed"
            rec.error = {"code": code, "message": str(exc)}
            rec.ended_at = time.time()
            logger.exception("adapter: unexpected failure job_id=%s error=%s", req.job_id, exc)
        finally:
            if rec.state in ("done", "failed", "cancelled"):
                ADAPTER_JOB_METRICS.on_job_finished(req.job_id, rec.state)
            try:
                if stage and stage.root.exists():
                    apply_stage_retention(
                        jobs_root=ADAPTER_JOB_PATH / "jobs",
                        current=stage.root,
                        keep=ADAPTER_KEEP_LAST_RESULTS,
                        logger=logger,
                    )
            except Exception as cleanup_err:
                logger.warning("adapter: stage cleanup error job_id=%s: %s", req.job_id, cleanup_err)

            # Small delay for resource cleanup (no GPU, but helps with file handles)
            await asyncio.sleep(0.5)

            self._active = max(0, self._active - 1)
            self._sem.release()


jobs = Jobs()

# -----------------------------------------------------------------------------
# FastAPI application
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Agnet Adapter",
    version="1.0.0",
    description="""
Agnet Adapter - Bridge to Agnet gRPC talking head video generation service.

This adapter translates HTTP requests from the async-2d-render worker to gRPC calls
to the Agnet render service. It enables seamless integration of Agnet's AI model
without modifying producer/worker code.

**Architecture Pattern:** Bridge (HTTP → gRPC → HTTP)
- Stateless, CPU-only processing
- No local inference engine (remote Agnet service handles GPU work)
- Image preprocessing (resize, JPG conversion, validation)
- Frame collection from gRPC stream
- ffmpeg encoding (frames + audio → MP4)

**Key Features:**
- Audio-driven talking head generation
- Automatic image preprocessing (max 1920px, JPG conversion)
- Real-time progress tracking (0-100%)
- Cooperative cancellation with watchdog safety net
- Temporary output storage (last 10 jobs)

**Workflow:**
1. POST /render/start with audio_url, photo_url
2. Poll GET /render/{job_id} for progress and state
3. Optional: POST /render/{job_id}/cancel to stop job
4. Download video from video_upload_url when state=done
5. Fallback: GET /render/{job_id}/output for cached file

**See also:** [Agnet API Documentation](/home/igor/repos/2d-render/docs/api.md)
""".strip()
)


@app.middleware("http")
async def request_logging_middleware(request, call_next):
    if request.url.path.startswith('/render/start'):
        logger.debug(
            "adapter: incoming %s %s client=%s",
            request.method,
            request.url.path,
            getattr(request.client, 'host', 'unknown'),
        )
        if LOG_SENSITIVE:
            body = await request.body()
            logger.debug("adapter: incoming payload preview=%s", body[:200])
    response = await call_next(request)
    if response.status_code >= 400 and request.url.path.startswith("/render"):
        logger.warning(
            "adapter: request failed %s %s -> %s",
            request.method, request.url.path, response.status_code
        )
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    logger.error(
        "REQUEST VALIDATION FAILED path=%s client=%s errors=%s",
        request.url.path,
        getattr(request.client, "host", "unknown"),
        exc.errors(),
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/healthz")
async def healthz():
    """Service liveness check - confirms adapter process is running and responsive."""
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    """Service readiness check - confirms dependencies are available."""
    checks: Dict[str, Any] = {
        "job_path_exists": ADAPTER_JOB_PATH.exists(),
    }
    ready = all(checks.values())
    if not ready:
        return JSONResponse(status_code=503, content={"ready": False, "checks": checks})
    return {"ready": True, "checks": checks}


@app.post("/render/start", response_model=StartResponse)
async def render_start(req: StartRequest):
    """
    Start a new talking head video generation job using Agnet gRPC service.

    This endpoint accepts audio and photo inputs, calls the remote Agnet service
    to generate video frames, encodes them with audio into MP4, and uploads the
    result to the provided upload URL.

    **Input Requirements:**
    - `audio_url`: Presigned URL to download audio file (WAV, MP3, etc.)
    - `photo_url`: Presigned URL to download photo (PNG, JPG, WebP) - OR -
    - `params.avatar_id`: ID of preset avatar (when using Agnet's preset avatars)
      - Note: Either photo_url OR avatar_id required (not both)
      - If both provided: avatar_id takes precedence, photo_url ignored
    - `video_upload_url`: Upload URL to write generated MP4 video
    - `job_id`: Unique identifier for this job (idempotent on duplicate)

    **Image Preprocessing:**
    - Max dimension: 1920px (auto-resized if larger)
    - Format conversion: PNG/WebP → JPG (configurable)
    - Size validation: < 10MB (gRPC message limit)
    - Dimension alignment: Multiples of 2

    **Video Output:**
    - Format: H.264 MP4, yuv420p (universal compatibility)
    - Resolution: Matches preprocessed image dimensions
    - Framerate: 25 fps (Agnet default)
    - Audio: Preserved from input

    **No Parameters:**
    Bridge pattern - no profile/params system (simplified)

    **Response States:**
    - `running`: Job is running (poll for progress)
    - `done`: Video generated and uploaded
    - `failed`: Job failed (check `error` field)
    - `cancelled`: Job cancelled before completion

    **See also:** GET /render/{job_id}, POST /render/{job_id}/cancel
    """
    logger.info(
        "adapter: received render/start job_id=%s",
        req.job_id,
    )
    logger.info(
        "adapter: render/start params summary job_id=%s has_photo_url=%s has_avatar_id=%s has_avatar=%s param_keys=%s",
        req.job_id,
        bool(req.inputs.photo_url),
        bool((req.params or {}).get("avatar_id")),
        isinstance((req.params or {}).get("avatar"), dict),
        sorted((req.params or {}).keys()),
    )
    avatar_obj = (req.params or {}).get("avatar")
    logger.info(
        "[AVATAR VERSION] app.render_start avatar.version=%s avatar_id=%s",
        avatar_obj.get("version") if isinstance(avatar_obj, dict) else "-",
        (req.params or {}).get("avatar_id") or "-",
    )

    # Validate: photo_url OR avatar reference required (Agnet supports preset avatars)
    params = req.params or {}
    avatar_id = params.get("avatar_id")
    avatar_ref = params.get("avatar")
    has_avatar_ref = isinstance(avatar_ref, dict) and bool(avatar_ref)
    photo_url = req.inputs.photo_url

    if not photo_url and not avatar_id and not has_avatar_ref:
        raise HTTPException(
            status_code=400,
            detail="Either inputs.photo_url, params.avatar_id, or params.avatar is required for Agnet adapter"
        )

    if photo_url and (avatar_id or has_avatar_ref):
        logger.warning(
            "adapter: Both photo_url and avatar reference provided for job_id=%s. Using avatar reference, ignoring photo_url",
            req.job_id,
        )
        req.inputs.photo_url = None  # Clear so avatar mode takes precedence

    if LOG_SENSITIVE:
        safe_payload = json.loads(req.model_dump_json())
        try:
            for key in ("audio_url", "photo_url"):
                if safe_payload["inputs"].get(key):
                    u = urlparse(safe_payload["inputs"][key])
                    safe_payload["inputs"][key] = f"{u.scheme}://{u.netloc}{u.path}"
            upload_url = (
                safe_payload["outputs"].get("video_upload_url")
                or safe_payload["outputs"].get("video_put_url")
            )
            if upload_url:
                v = urlparse(upload_url)
                safe_payload["outputs"]["video_upload_url"] = f"{v.scheme}://{v.netloc}{v.path}"
            if safe_payload.get("transfer_auth"):
                # Never log credential values; mask the name too for symmetry.
                safe_payload["transfer_auth"] = {
                    "header_name": safe_payload["transfer_auth"].get("header_name", "***"),
                    "header_value": "***",
                }
        except Exception:
            pass
        logger.debug(
            "adapter: render/start payload job_id=%s payload=%s",
            req.job_id,
            json.dumps(safe_payload, ensure_ascii=False),
        )

    rec = await jobs.start_or_get(req)
    logger.info("adapter: render/start response job_id=%s state=%s", rec.job_id, rec.state)
    return StartResponse(job_id=rec.job_id, state=rec.state, outputs=rec.outputs, error=rec.error)


@app.get("/render/{job_id}", response_model=PollResponse)
async def render_poll(job_id: str):
    """
    Poll job status and progress - check current state of render job.

    **Response Fields:**
    - `state`: Current job state (running/done/failed/cancelled)
    - `progress`: Integer 0-100 indicating completion percentage
    - `outputs`: Output references (video_key, etc.)
    - `error`: Error information if state is "failed"
    - `cancel_requested`: Boolean flag for running jobs

    **Polling Best Practices:**
    - Recommended interval: 2-5 seconds
    - Stop when state is final (done/failed/cancelled)
    - Handle 404 errors (job not found or expired)

    **See also:** POST /render/start, POST /render/{job_id}/cancel
    """
    rec = await jobs.get_by_job(job_id)
    if not rec:
        logger.warning("adapter: render poll job not found job_id=%s", job_id)
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "unknown job_id"}}
        )

    try:
        rec.poll_count = int(getattr(rec, "poll_count", 0)) + 1
    except Exception:
        rec.poll_count = 1

    response = PollResponse(
        state=rec.state,
        progress=rec.progress,
        outputs=rec.outputs,
        error=rec.error,
        cancel_requested=rec.cancel_requested if rec.state == "running" else None,
    )
    logger.info(
        "adapter: render poll job_id=%s state=%s progress=%s error=%s poll#=%d",
        job_id,
        rec.state,
        rec.progress,
        bool(rec.error),
        rec.poll_count,
    )
    return response


@app.post("/render/{job_id}/cancel", response_model=PollResponse)
async def render_cancel(job_id: str):
    """
    Request cooperative cancellation of a running job.

    Initiates graceful job termination via gRPC stream cancellation.
    Typical latency: <2 seconds.

    **See also:** POST /render/start, GET /render/{job_id}
    """
    rec = await jobs.request_cancel(job_id)
    logger.info(
        "adapter: cancel requested job_id=%s state=%s already_cancelled=%s",
        job_id,
        rec.state,
        rec.cancel_requested,
    )
    return PollResponse(
        state=rec.state,
        progress=rec.progress,
        outputs=rec.outputs,
        error=rec.error,
        cancel_requested=rec.cancel_requested,
    )


@app.get("/render/{job_id}/output")
async def render_get_output(job_id: str):
    """
    Retrieve cached output video file for a completed job.

    Fallback access when S3 upload fails or for debugging.
    Files retained according to ADAPTER_KEEP_LAST_RESULTS (default: 10 jobs).

    **See also:** POST /render/start, GET /render/{job_id}
    """
    output_path = ADAPTER_JOB_PATH / "jobs" / job_id / "outputs" / "video.mp4"

    if not output_path.exists():
        logger.warning("adapter: output file not found job_id=%s path=%s", job_id, output_path)
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "OUTPUT_NOT_FOUND",
                    "message": f"Output file not found for job_id={job_id}. "
                               f"File may have been pruned or job hasn't completed."
                }
            }
        )

    logger.info(
        "adapter: serving cached output job_id=%s path=%s size=%d",
        job_id, output_path, output_path.stat().st_size
    )

    return FileResponse(
        path=str(output_path),
        media_type="video/mp4",
        filename=f"{job_id}.mp4"
    )

HTTP_METRICS_REGISTRY = install_http_metrics(app, service="adapter-agnet")
ADAPTER_JOB_METRICS = AdapterJobMetrics(
    registry=HTTP_METRICS_REGISTRY,
    service="adapter-agnet",
    engine="agnet",
)
AGNET_BRIDGE_METRICS = AgnetBridgeMetrics(
    registry=HTTP_METRICS_REGISTRY,
    service="adapter-agnet",
    engine="agnet",
)
