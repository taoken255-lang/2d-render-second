from fastapi import FastAPI
from rtc_mediaserver.api import offline, online

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

app = FastAPI()

# Exception handler for invalid JSON
@app.exception_handler(JSONDecodeError)
async def json_decode_error_handler(request: Request, exc: JSONDecodeError):
    return JSONResponse(
        status_code=400,
        content={
            "type": "error",
            "code": "UNKNOWN_ERROR",
            "message": "Unknown error occured."
        }
    )


origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup_event() -> None:
    """Launch gRPC stream worker and isolated WebRTC thread on app startup."""
    # Запускаем изолированный WebRTC поток
    webrtc_manager.start()

    # Запускаем gRPC worker в главном loop
    t = asyncio.create_task(stream_worker_forever())
    logger.info("gRPC aio worker task created (auto-restart enabled)")
    logger.info("🚀 Isolated WebRTC thread started")
    if settings.watchdog_enabled:
        wt = asyncio.create_task(watchdog())


@app.on_event("shutdown")
async def _shutdown() -> None:
    logger.info("Application shutdown – stopping WebRTC thread")
    # Останавливаем изолированный WebRTC поток
    webrtc_manager.stop()
    logger.info("Application shutdown complete")

app.include_router(online.router)
app.include_router(offline.router)