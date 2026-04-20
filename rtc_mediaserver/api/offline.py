"""FastAPI application exposing WebRTC endpoints and single control websocket."""
from __future__ import annotations

import asyncio
import json
import os
import uuid

from json import JSONDecodeError
from pathlib import Path
from fastapi import APIRouter
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCDataChannel, RTCConfiguration  # type: ignore
from aiortc.rtcrtpsender import RTCRtpSender  # type: ignore
from pydantic import BaseModel, UUID4
from rtc_mediaserver.common.tools import cleanup_old_results
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from rtc_mediaserver.common.logging_config import get_logger, setup_default_logging
from rtc_mediaserver.render.offline.client import local_video_run
from rtc_mediaserver.common.task_manager import TASK_MANAGER
from rtc_mediaserver.common.constants import CAN_SEND_FRAMES, RTC_STREAM_CONNECTED, WS_CONTROL_CONNECTED, USER_EVENTS, \
    AVATAR_SET, INIT_DONE, \
    STATE
from rtc_mediaserver.render.online.client import stream_worker_forever
from rtc_mediaserver.api.handlers import HANDLERS, ClientState
from rtc_mediaserver.render.common.info import info
from rtc_mediaserver.services.tts.elevenlabs import voices
from rtc_mediaserver.common.util import wav_to_mono_and_sample_rate
from rtc_mediaserver.common.watchdog import watchdog
from rtc_mediaserver.webrtc.webrtc_manager import webrtc_manager
from rtc_mediaserver.common.config import settings

# Ensure logging configured
setup_default_logging()
logger = get_logger(__name__)

router = APIRouter()

class RenderResponseData(BaseModel):
    job_id: UUID4


class CommonResponse(BaseModel):
    detail: str


async def start_render_task(
        audio: bytes,
        sample_rate: int,
        bps: int,
        avatar_id: str,
        output_path: Path,
        request_id: UUID4,
        audio_fmt: str,
        tail_video_path: Path | None = None,
):
    try:
        logger.info(
            "start_render_task request_id=%s avatar=%s output_path=%s tail_video_path=%s",
            request_id,
            avatar_id,
            output_path,
            tail_video_path,
        )
        await local_video_run(
            audio=audio,
            sample_rate=sample_rate,
            bps=bps,
            avatar_id=avatar_id,
            output_path=output_path,
            audio_fmt=audio_fmt,
            tail_video_path=tail_video_path,
        )
        await TASK_MANAGER.set_status(task_id=str(request_id), status="done")
    except Exception as exc:
        logger.error(exc)
        await TASK_MANAGER.set_status(task_id=str(request_id), status="error")


def _resolve_tail_video_path(tail_video_name: str | None) -> Path | None:
    if not tail_video_name:
        logger.info("Tail video name is empty, tail concat disabled")
        return None

    logger.info("Resolving tail video name: %s", tail_video_name)
    candidate = Path(tail_video_name)
    # Accept only bare file names without path segments or suffixes.
    if candidate.name != tail_video_name or candidate.suffix:
        logger.error("Invalid tail video name: %s", tail_video_name)
        raise ValueError("Tail video name must not contain directories or extension.")

    resolved = settings.offline_tail_videos_path / f"{tail_video_name}.mp4"
    logger.info("Resolved tail video path candidate: %s", resolved)
    if not resolved.exists():
        logger.error("Tail video file does not exist: %s", resolved)
        raise FileNotFoundError(f"Tail video is not found: {resolved}")

    logger.info("Tail video file found: %s", resolved)
    return resolved


@router.post("/render")
async def render(
        response: Response,
        avatar: str = Form(),
        audio: UploadFile = File(None),
        tail_video_name: str = Form(None)
):
    if TASK_MANAGER.is_locked():
        return JSONResponse(status_code=400, content={
            "error": "SERVER_BUSY",
            "description": "Server is busy."
        })
    if avatar not in settings.offline_avatars:
        avatar = "iirina"

    try:
        if audio is None:
            logger.info("audio is empty")
            return JSONResponse(status_code=400, content={
                "error": "WRONG_INPUT",
                "description": "Wrong input."
            })
        else:
            _, audio_ext = os.path.splitext(audio.filename)
            logger.info(
                "render request avatar=%s audio_filename=%s tail_video_name=%s",
                avatar,
                audio.filename,
                tail_video_name,
            )
            request_audio = await audio.read()
            sample_rate, audio_fmt, mono_audio = wav_to_mono_and_sample_rate(request_audio)
            if not sample_rate or not mono_audio:
                logger.info("audio decode error")
                return JSONResponse(status_code=400, content={
                    "error": "WRONG_INPUT",
                    "description": "Wrong input."
                })
            logger.info(f"Audio size: {len(request_audio)}, SR: {sample_rate}")
            request_id = uuid.uuid4()
            try:
                tail_video_path = _resolve_tail_video_path(tail_video_name)
            except (ValueError, FileNotFoundError) as exc:
                logger.error("Tail video resolve failed: %r", exc)
                return JSONResponse(status_code=400, content={
                    "error": "WRONG_INPUT",
                    "description": str(exc)
                })
            logger.info("Tail video path for request_id=%s: %s", request_id, tail_video_path)

            if not settings.offline_output_path.exists():
                settings.offline_output_path.mkdir(exist_ok=True, parents=True)

            output_path = settings.offline_output_path / str(request_id)
            await TASK_MANAGER.set_status(task_id=str(request_id), status="processing")

            t = asyncio.create_task(start_render_task(
                audio=mono_audio,
                sample_rate=sample_rate,
                bps=16,
                avatar_id=avatar,
                output_path=output_path,
                request_id=request_id,
                audio_fmt=audio_fmt,
                tail_video_path=tail_video_path,
            ))
            TASK_MANAGER.set_task(t, job_id=str(request_id))

            response.status_code = 200
            response_model = RenderResponseData(job_id=request_id)
            return response_model
    except Exception as exc:
        logger.error(exc)
        return JSONResponse(status_code=400, content={
            "error": "UNKNOWN_ERROR",
            "description": "Unknown error occured."
        })


@router.get("/render/status/{job_id}")
async def status(job_id: str):
    task_status = TASK_MANAGER.get(task_id=job_id)
    if task_status is None:
        return JSONResponse(status_code=400, content={
            "error": "UNKNOWN_JOB_ID",
            "description": "Job Id is not found."
        })
    return task_status


@router.get("/render/result/{task_id}")
async def get_result(task_id: str):
    """
    Возвращает итоговое видео по task_id.
    """
    task = TASK_MANAGER.get(task_id)
    if not task:
        return JSONResponse(status_code=400, content={
            "error": "UNKNOWN_JOB_ID",
            "description": "Job Id is not found."
        })

    if task.get("status") != "done":
        return JSONResponse(status_code=400, content={
            "error": "BAD_STATUS",
            "description": "Requested action can not be performed for job in status <status>."
        })

    result_path = settings.offline_output_path / task_id / "video.mp4"
    if not result_path or not Path(result_path).exists():
        return JSONResponse(status_code=400, content={
            "error": "UNKNOWN_ERROR",
            "description": "Unknown error occured."
        })

    return FileResponse(
        result_path,
        media_type="video/mp4",
        filename=f"{task_id}.mp4"
    )


@router.delete("/render/{job_id}")
async def abort_render(job_id: str):
    logger.info(f"Request to abort task {job_id}")

    task = TASK_MANAGER.get(job_id)
    if not task:
        return JSONResponse(status_code=400, content={
            "error": "UNKNOWN_JOB_ID",
            "description": "Job Id is not found."
        })

    if task.get("status") != "processing":
        return JSONResponse(status_code=400, content={
            "error": "BAD_STATUS",
            "description": "Requested action can not be performed for job in status <status>."
        })

    await TASK_MANAGER.cancel_task(job_id)
    await cleanup_old_results()
