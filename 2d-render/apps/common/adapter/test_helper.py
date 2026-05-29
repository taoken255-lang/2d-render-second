from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional, Dict
from apps.common.helpers import env_int
from apps.common.adapter.stage import Stage
from urllib.request import Request, build_opener, HTTPHandler


async def _range_probe(
    url: Optional[str],
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
) -> None:
    if not url:
        return

    def _do() -> None:
        req = Request(url, method="GET")
        req.add_header("Range", "bytes=0-0")
        for key, value in (headers or {}).items():
            k = str(key or "").strip()
            v = str(value or "").strip()
            if k and v:
                req.add_header(k, v)
        opener = build_opener(HTTPHandler())
        with opener.open(req, timeout=timeout) as resp:
            if resp.status not in (200, 206):
                raise RuntimeError(f"probe unexpected status {resp.status}")
    await asyncio.to_thread(_do)


async def _http_put(
    url: str,
    data: bytes,
    content_type: str,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
) -> None:
    def _do() -> None:
        req = Request(url, data=data, method="PUT")
        req.add_header("Content-Type", content_type or "application/octet-stream")
        for key, value in (headers or {}).items():
            k = str(key or "").strip()
            v = str(value or "").strip()
            if k and v:
                req.add_header(k, v)
        opener = build_opener(HTTPHandler())
        with opener.open(req, timeout=timeout) as resp:
            if resp.status not in (200, 201, 204):
                raise RuntimeError(f"put unexpected status {resp.status}")
    await asyncio.to_thread(_do)


async def run_self_test(
    *,
    req: Any,
    rec: Any,
    tick_sec: float,
    http_timeout: float,
    work_root: Path,
    logger,
) -> None:
    """
    Mock adapter pipeline used when ADAPTER_TEST_MODE=1.

    - Validates inputs via tiny Range GETs
    - Emits progress ticks up to 99%
    - Creates stage directory and writes fake video locally
    - Uploads fake video via worker-provided upload URL
    - Marks job as done, or failed with a descriptive error

    The local file write enables testing of the cached output endpoint
    (GET /render/{job_id}/output) without full inference.
    """
    try:
        logger.info("adapter[test]: validating input URLs job_id=%s", req.job_id)
        auth_headers: Optional[Dict[str, str]] = None
        if getattr(req, "transfer_auth", None):
            auth_headers = {
                req.transfer_auth.header_name: req.transfer_auth.header_value,
            }
        await _range_probe(
            req.inputs.audio_url,
            http_timeout,
            headers=auth_headers,
        )
        await _range_probe(
            req.inputs.photo_url,
            http_timeout,
            headers=auth_headers,
        )

        # Progress increments: per tick, add ADAPTER_MOCK_PROGRESS percent points (default 10)
        inc = env_int("ADAPTER_MOCK_PROGRESS", env_int("ADAPTER_MOCK_STEPS", 10))
        if inc < 1:
            inc = 1
        if inc > 99:
            inc = 99
        while rec.progress < 99:
            rec.progress = min(99, rec.progress + inc)
            await asyncio.sleep(tick_sec)

        # Create stage directory structure for this job
        stage = Stage.create(work_root, req.job_id)
        logger.info("adapter[test]: created stage at %s", stage.root)

        # Generate fake video content
        fake = b"OMNI_MP4\x00" * 512  # ~4 KB

        # Write fake video to local stage (enables cached endpoint testing)
        output_path = stage.outputs / "video.mp4"
        await asyncio.to_thread(output_path.write_bytes, fake)
        logger.info("adapter[test]: wrote fake video to %s (%d bytes)", output_path, len(fake))

        # Upload to the worker-provided target (data plane in secure mode,
        # direct-S3 only in explicit compatibility flows).
        await _http_put(
            req.outputs.video_upload_url,
            fake,
            req.outputs.content_type or "video/mp4",
            http_timeout,
            headers=auth_headers,
        )

        rec.progress = 100
        rec.state = "done"
        rec.ended_at = time.time()
        logger.info("adapter[test]: job completed job_id=%s duration=%.1fs", req.job_id, rec.ended_at - rec.started_at)

    except Exception as e:
        rec.state = "failed"
        rec.ended_at = time.time()
        msg = str(e)
        if "404" in msg or "Not Found" in msg:
            rec.error = {
                "code": "INVALID_INPUT_URL",
                "message": f"Input URL validation failed: {msg}. URLs may be unreachable or invalid (expected for tests)",
            }
            logger.error(
                "adapter[test]: invalid input URLs job_id=%s error=%s duration=%.1fs",
                req.job_id,
                msg,
                rec.ended_at - rec.started_at,
            )
        else:
            rec.error = {"code": "ENGINE_ERROR", "message": msg}
            logger.error("adapter[test]: job failed job_id=%s error=%s duration=%.1fs", req.job_id, msg, rec.ended_at - rec.started_at)
