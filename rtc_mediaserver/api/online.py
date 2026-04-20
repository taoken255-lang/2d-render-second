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




@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:  # type: ignore[override]
    if not settings.debug_page_enabled:
        return Response(status_code=200, content="DEBUG PAGE DISABLED")

    HTML_FILE = Path(__file__).parent.parent / "templates" / "index.html"
    return HTMLResponse(HTML_FILE.read_text(encoding="utf-8"))


@router.get("/info")
async def get_info() -> JSONResponse:
    """Get available avatars with their animations and emotions."""
    try:
        info_data = await info()
        return JSONResponse(info_data)
    except Exception as e:
        logger.error(f"Error getting info data: {e}")
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "code": "UNKNOWN_ERROR",
                "message": "Unknown error occured."
            }
        )


@router.post("/offer")
async def offer(request: Request,
                bitrate: int = settings.bitrate,
                turn: bool = settings.turn_enabled
                ):  # type: ignore[override]
    webrtc_manager.main_loop = asyncio.get_running_loop()

    # Check Content-Type header
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("application/json"):
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "code": "UNKNOWN_ERROR",
                "message": "Unknown error occured."
            }
        )

    if RTC_STREAM_CONNECTED.locked():
        if STATE.current_pc:
            try:
                logger.info(f"New connection attempt, killing old connection")
                await STATE.current_pc.close()
            except BaseException as e:
                logger.error(e)

    if bitrate:
        logger.info(f"Bitrate set to {bitrate} kbps")
        import aiortc.codecs.h264

        setattr(aiortc.codecs.h264, "DEFAULT_BITRATE", bitrate)
        setattr(aiortc.codecs.h264, "MAX_BITRATE", bitrate)
        setattr(aiortc.codecs.h264, "MIN_BITRATE", bitrate)

    if turn:
        logger.info(f"TURN enabled")

    try:
        params = await request.json()
    except JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "code": "UNKNOWN_ERROR",
                "message": "Unknown error occured."
            }
        )

    request_type = params.get("type", None)
    sdp = params.get("sdp", None)

    if not params or not request_type or not sdp or request_type != "offer":
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "code": "UNKNOWN_ERROR",
                "message": "Unknown error occured."
            }
        )

    try:
        # 🚀 Используем изолированный WebRTC поток
        logger.info("Processing WebRTC offer in isolated thread")
        answer_dict = await webrtc_manager.process_offer_async(params, turn)
        logger.info("WebRTC offer processed successfully")
        return JSONResponse(answer_dict)
    except Exception as e:
        logger.error(f"❌ WebRTC offer processing failed: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "type": "error",
                "code": "WEBRTC_ERROR",
                "message": f"WebRTC processing failed: {str(e)}"
            }
        )


# ───────────────────────── Control websocket ───────────────────────────

async def send_user_event(websocket: WebSocket):
    logger.info("Started user events watchdog")
    while True:
        message = await USER_EVENTS.get()
        logger.info(f"Send event {message}")
        await websocket.send_json(message)


@router.get("/health")
async def health():
    try:
        await voices()
    except:
        return Response(status_code=500)
    return Response(status_code=200)


@router.websocket("/ws")
async def control_ws(websocket: WebSocket):  # type: ignore[override]
    """Single websocket channel handling control/audio messages."""
    await websocket.accept()

    if WS_CONTROL_CONNECTED.locked():
        await websocket.send_json({
            "type": "error",
            "code": "SERVICE_BUSY",
            "message": "Reached max count of connected clients. Service busy."
        })
        await websocket.close()
        return

    try:
        await asyncio.wait_for(WS_CONTROL_CONNECTED.acquire(), 0.1)
    except asyncio.TimeoutError:
        await websocket.send_json({
            "type": "error",
            "code": "SERVICE_BUSY",
            "message": "Reached max count of connected clients. Service busy."
        })
        await websocket.close()
        return

    state = ClientState()

    eos_watcher = asyncio.create_task(send_user_event(websocket))

    try:
        while True:
            data_text = await websocket.receive_text()
            try:
                message = json.loads(data_text)
                msg_type = message.get("type")
                if msg_type is None:
                    raise ValueError("Message missing 'type' field")
                handler = HANDLERS.get(msg_type)
                if handler is None:
                    raise ValueError(f"Unknown message type '{msg_type}'")
                logger.info("Received WS command: %s", msg_type)
                result = await handler(message, state)
                if isinstance(result, dict):
                    await websocket.send_json(result)
            except json.JSONDecodeError as exc:
                logger.exception(f"Error processing WS message - invalid JSON: {data_text}, {exc!r}")
                await websocket.send_json({
                    "type": "error",
                    "code": "INVALID_JSON",
                    "message": f"'{data_text}' is not valid JSON."
                })
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Error processing WS message, {data_text}, {exc!r}")
                await websocket.send_json({
                    "type": "error",
                    "code": "UNKNOWN_ERROR",
                    "message": "Unknown error occured."
                })
    except WebSocketDisconnect as e:
        logger.warning(f"Control websocket disconnected, code: {e.code}, reason: {e.reason}")
    except BaseException as e:
        logger.error(f"Control websocket error occured! {e!r}")
    finally:
        eos_watcher.cancel()
        try:
            WS_CONTROL_CONNECTED.release()
        except ValueError as e:
            logger.error(f"WS_CONTROL_CONNECTED.release() -> {e!r}")
        INIT_DONE.clear()
        if not CAN_SEND_FRAMES.is_set():
            STATE.avatar = None
            AVATAR_SET.clear()
