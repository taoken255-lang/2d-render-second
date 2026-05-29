"""
Shared Pydantic models and dataclasses for adapter HTTP contracts.

These models define the standard request/response schemas used by all adapters.
Workers and producers rely on this contract for consistency.

Strict-by-design: every adapter-contract model sets `extra="forbid"`. Worker↔adapter
is internal RPC between co-developed services, same trust boundary as the data-plane
internal control bodies in `apps/data_plane/api.py` (`AvatarRegisterBody` &c.).
Unknown fields are typos, not forward-compat, and must fail loud.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Optional, Literal, Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class StrictModel(BaseModel):
    """Base for adapter-contract models. Rejects unknown fields."""
    model_config = ConfigDict(extra="forbid")


class TransferAuth(StrictModel):
    """
    Auth credential carried in the adapter payload for data-plane transfers.

    Worker tells the adapter both the header name and value so the adapter
    stays ignorant of data-plane env config. Adapter must attach this header
    to every audio_url / photo_url GET and every video_upload_url PUT.

    Absent when worker is in direct-S3 compat mode (URLs carry presigned auth).
    Both fields must be non-empty when present — fail loud on misconfigured
    payloads rather than silently sending unauthed requests.
    """
    header_name: str
    header_value: str

    @field_validator("header_name", "header_value")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be non-empty")
        return v


class StartInputs(StrictModel):
    """
    Input URLs for rendering job.

    Attributes:
        prompt: Optional text prompt (not all engines use text prompts)
        audio_url: Download URL for audio input (data plane or direct-S3 compat)
        photo_url: Optional download URL for image input - optional for engines with preset avatars
        audio_filename: Canonical staged filename for audio input
        photo_filename: Canonical staged filename for photo input when present
    """
    prompt: Optional[str] = None  # Made optional for engines that don't use text prompts
    audio_url: str
    photo_url: Optional[str] = None  # Made optional for engines with preset avatars
    audio_filename: Optional[str] = None
    photo_filename: Optional[str] = None

    @field_validator("audio_url", "photo_url")
    @classmethod
    def _valid_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        u = urlparse(v)
        if u.scheme not in ("http", "https", "file"):
            raise ValueError("must be http(s) URL or file:// path")
        return v


class StartOutputs(StrictModel):
    """
    Output destination for rendering job.

    Attributes:
        video_key: S3 object key for result (e.g., "jobs/123/video.mp4")
        video_upload_url: Upload URL for writing result (data plane or direct-S3)
        content_type: MIME type for output (default: video/mp4)
    """
    video_key: str
    video_upload_url: str
    content_type: Optional[str] = "video/mp4"

    # Normalize legacy payloads before field validation:
    # accept `video_put_url` from older producers and map it to `video_upload_url`.
    # `mode="before"` runs before `extra="forbid"`, so the alias still works.
    # Always strip the legacy key after normalization so strict validation passes.
    @model_validator(mode="before")
    @classmethod
    def _compat_video_put_url(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "video_put_url" not in data:
            return data
        patched = dict(data)
        legacy = patched.pop("video_put_url", None)
        if not patched.get("video_upload_url") and isinstance(legacy, str) and legacy.strip():
            patched["video_upload_url"] = legacy
        return patched

    @property
    def video_put_url(self) -> str:
        """
        Deprecated alias for compatibility with older call sites.
        """
        return self.video_upload_url

    @field_validator("video_upload_url")
    @classmethod
    def _valid_url(cls, v: str) -> str:
        u = urlparse(v)
        if u.scheme not in ("http", "https", "file"):
            raise ValueError("must be http(s) URL or file:// path")
        return v


class StartRequest(StrictModel):
    """
    Request payload for /render/start endpoint.

    Attributes:
        job_id: Unique identifier for this job (worker-generated UUID)
        inputs: Input URLs (audio, photo, optional prompt)
        outputs: Output destination (upload target URL)
        transfer_auth: Optional auth credential the adapter must attach to every
            audio_url / photo_url GET and every video_upload_url PUT. Present when
            worker is in data-plane mode; absent when CLIENT_S3_DIRECT_ENABLED=true.
        profile: Optional profile name for parameter presets (e.g., "fast", "quality")
        params: Optional raw parameter overrides (engine-specific)
    """
    job_id: str
    inputs: StartInputs
    outputs: StartOutputs
    transfer_auth: Optional[TransferAuth] = None
    profile: Optional[str] = None
    params: Optional[dict] = None

    @field_validator("job_id")
    @classmethod
    def _non_empty_job(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("job_id must be non-empty")
        return v


class StartResponse(BaseModel):
    """
    Response from /render/start endpoint.

    Attributes:
        job_id: Echo back the job ID
        state: Current job state (running/done/failed/cancelled)
        outputs: Output metadata (e.g., video_key)
        error: Error details if state is failed
    """
    job_id: str
    state: Literal["running", "done", "failed", "cancelled"]
    outputs: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None


class PollResponse(BaseModel):
    """
    Response from /render/{job_id} polling endpoint.

    Attributes:
        state: Current job state
        progress: Percentage complete (0-100)
        outputs: Output metadata when done
        error: Error details if failed
        cancel_requested: Whether cancel was requested (for debugging)
    """
    state: Literal["running", "done", "failed", "cancelled"]
    progress: Optional[int] = None
    outputs: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    cancel_requested: Optional[bool] = None


@dataclass
class JobRec:
    """
    In-memory job record for adapter job registry.

    Attributes:
        job_id: Unique job identifier
        state: Current state (running/done/failed/cancelled)
        progress: Percentage complete (0-100)
        error: Error details (code, message, stderr_tail)
        outputs: Output metadata (video_key, etc.)
        params: Effective parameters for this job (profile + overrides)
        started_at: Unix timestamp when job started
        ended_at: Unix timestamp when job finished
        task: Asyncio task handle for job execution
        cancel_requested: Whether cancel was requested
        cancel_event: Asyncio event for cancel signaling
        poll_count: Number of times /render/{job_id} was polled
    """
    job_id: str
    state: Literal["running", "done", "failed", "cancelled"]
    progress: int = 0
    error: Optional[Dict[str, Any]] = None
    outputs: Optional[Dict[str, Any]] = None
    params: Optional[Dict[str, Any]] = None
    started_at: float = 0.0
    ended_at: Optional[float] = None
    task: Optional[asyncio.Task] = None
    cancel_requested: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    poll_count: int = 0
