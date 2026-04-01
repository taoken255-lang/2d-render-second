"""
WebRTC Manager - изолированный WebRTC в отдельном потоке
"""
import asyncio
import threading
import time
import logging
from asyncio import AbstractEventLoop
from concurrent.futures import Future
from typing import Dict, Any, Optional
from dataclasses import dataclass

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.rtcrtpsender import RTCRtpSender

from .player import WebRTCMediaPlayer
from .constants import RTC_STREAM_CONNECTED, CAN_SEND_FRAMES, AVATAR_SET, STATE, State, WS_CONTROL_CONNECTED, \
    SENTENCES_QUEUE
from .shared import AUDIO_SECOND_QUEUE
from .tts.elevenlabs import start_synthesize_worker, stop_synthesize_worker
from ..config import settings

logger = logging.getLogger(__name__)

@dataclass
class OfferRequest:
    """Запрос на обработку WebRTC offer"""
    sdp: str
    type: str
    session_id: int
    turn: bool

@dataclass  
class OfferResponse:
    """Ответ на WebRTC offer"""
    sdp: str
    type: str
    success: bool
    error: Optional[str] = None

def sdp_set_bandwidth(sdp: str, *, video_kbps: int = 4000, audio_kbps: int = 128, framerate: int = 25) -> str:
    """Простой SDP-мунджер: задаёт b=AS для audio/video и a=framerate для видео."""
    # Копируем логику из api.py
    lines = sdp.splitlines()
    out = []
    in_video = False
    in_audio = False

    def inject_video_params(dst: list):
        while dst and (dst[-1].startswith("b=AS:") or dst[-1].startswith("b=TIAS:") or dst[-1].startswith("a=framerate:")):
            dst.pop()
        dst.append(f"b=AS:{video_kbps}")
        dst.append(f"a=framerate:{framerate}")

    def inject_audio_params(dst: list):
        while dst and (dst[-1].startswith("b=AS:") or dst[-1].startswith("b=TIAS:")):
            dst.pop()
        dst.append(f"b=AS:{audio_kbps}")

    for i, ln in enumerate(lines):
        if ln.startswith("m=video"):
            in_video, in_audio = True, False
            out.append(ln)
            continue
        if ln.startswith("m=audio"):
            in_video, in_audio = False, True
            out.append(ln)
            continue
        if ln.startswith("m="):
            if in_video:
                inject_video_params(out)
            if in_audio:
                inject_audio_params(out)
            in_video = in_audio = False
            out.append(ln)
            continue
        out.append(ln)

    if in_video:
        inject_video_params(out)
    if in_audio:
        inject_audio_params(out)

    return "\r\n".join(out) + "\r\n"

class WebRTCManager:
    """Менеджер изолированного WebRTC в отдельном потоке"""
    
    def __init__(self):
        self.webrtc_thread: Optional[threading.Thread] = None
        self.webrtc_loop: Optional[asyncio.AbstractEventLoop] = None
        self.running = False
        self.main_loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Очередь для межпотоковой коммуникации
        self.request_queue: asyncio.Queue[tuple[OfferRequest, Future]] = None
        
    def start(self):
        """Запуск WebRTC потока"""
        if self.running:
            return
            
        logger.info("🚀 Starting isolated WebRTC thread...")

        self.running = True
        self.webrtc_thread = threading.Thread(
            target=self._webrtc_worker,
            name="WebRTC-Isolated",
            daemon=False
        )
        self.webrtc_thread.start()
        
        # Ждем инициализации loop
        while self.webrtc_loop is None:
            time.sleep(0.001)
            
        logger.info("✅ WebRTC thread started successfully")
    
    def stop(self):
        """Остановка WebRTC потока"""
        if not self.running:
            return
            
        logger.info("🛑 Stopping WebRTC thread...")
        self.running = False
        
        if self.webrtc_loop:
            # Остановка loop из другого потока
            self.webrtc_loop.call_soon_threadsafe(self.webrtc_loop.stop)
            
        if self.webrtc_thread:
            self.webrtc_thread.join(timeout=5.0)
            
        logger.info("✅ WebRTC thread stopped")
    
    def _webrtc_worker(self):
        """Основной worker WebRTC потока"""
        # Создаем новый event loop для WebRTC
        self.webrtc_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.webrtc_loop)
        
        # Повышаем приоритет потока (если возможно)
        try:
            import os
            if hasattr(os, 'nice'):
                os.nice(-5)  # Повышаем приоритет
        except:
            pass
        
        logger.info("🎵 WebRTC event loop initialized")
        
        # Запускаем основную coroutine
        try:
            self.webrtc_loop.run_until_complete(self._webrtc_main())
        except Exception as e:
            logger.error(f"WebRTC worker crashed: {e}")
        finally:
            self.webrtc_loop.close()
            self.webrtc_loop = None
    
    async def _webrtc_main(self):
        """Основная логика WebRTC потока"""
        # Создаем очередь для запросов
        self.request_queue = asyncio.Queue(maxsize=10)
        
        logger.info("🎵 WebRTC main loop started")
        
        # Обрабатываем запросы на создание peer connections
        while self.running:
            try:
                # Ждем запрос с таймаутом
                request, future = await self.request_queue.get()
                
                # Обрабатываем offer
                try:
                    response = await self._process_offer_isolated(request)
                    # Возвращаем результат в главный поток
                    self.webrtc_loop.call_soon_threadsafe(
                        future.set_result, response
                    )
                except Exception as e:
                    logger.error(f"Error processing offer: {e}")
                    error_response = OfferResponse(
                        sdp="", type="", success=False, error=str(e)
                    )
                    self.webrtc_loop.call_soon_threadsafe(
                        future.set_result, error_response
                    )
            except Exception as e:
                logger.error(f"WebRTC main loop error: {e}")
                
        logger.info("🎵 WebRTC main loop stopped")
    
    async def _process_offer_isolated(self, request: OfferRequest) -> OfferResponse:
        """Обработка WebRTC offer в изолированном потоке"""
        try:
            offer = RTCSessionDescription(sdp=request.sdp, type=request.type)
            use_turn = settings.turn_enabled or request.turn
            if use_turn:
                pc = RTCPeerConnection(
                    RTCConfiguration(
                        iceServers=[
                            RTCIceServer(
                                urls=f"turn:{settings.turn_server}?transport=udp",
                                username=settings.turn_login,
                                credential=settings.turn_password,
                            )
                        ]
                    )
                )
            else:
                pc = RTCPeerConnection()

            # Создаем медиа плеер в WebRTC потоке
            player = WebRTCMediaPlayer()
            player.main_loop = self.main_loop
            pc.addTrack(player.audio)
            pc.addTrack(player.video)

            def force_codec(pc: RTCPeerConnection, sender: RTCRtpSender, forced_codec: str) -> None:
                kind = forced_codec.split("/")[0]
                codecs = RTCRtpSender.getCapabilities(kind).codecs
                transceiver = next(t for t in pc.getTransceivers() if t.sender == sender)
                transceiver.setCodecPreferences(
                    [codec for codec in codecs if codec.mimeType == forced_codec]
                )

            # Настройка кодеков
            for t in pc.getTransceivers():
                if t.kind == "video":
                    caps = RTCRtpSender.getCapabilities("video")

                    preferences = list(filter(lambda x: x.name == "H264", caps.codecs))
                    for preference in preferences:
                        preference.parameters["profile-level-id"] = "42e028"
                    transceiver = pc.getTransceivers()[1]
                    transceiver.setCodecPreferences(preferences)

                    h264_pmode1 = [
                        c for c in caps.codecs
                        if c.name == "H264" and c.parameters.get("packetization-mode") == "1"
                    ]
                    if h264_pmode1:
                        t.setCodecPreferences(h264_pmode1)

            await pc.setRemoteDescription(offer)
            
            # Создаем answer
            answer = await pc.createAnswer()
            munged_sdp = sdp_set_bandwidth(
                answer.sdp,
                video_kbps=4000,
                audio_kbps=128,
                framerate=25,
            )
            await pc.setLocalDescription(RTCSessionDescription(sdp=munged_sdp, type=answer.type))
            
            logger.info(f"✅ WebRTC session {request.session_id} established in isolated thread")
            
            # Настройка обработчиков событий
            await self._setup_connection_handlers(pc, request.session_id)
            
            return OfferResponse(
                sdp=pc.localDescription.sdp,
                type=pc.localDescription.type,
                success=True
            )
            
        except Exception as e:
            logger.error(f"Error in isolated offer processing: {e}")
            return OfferResponse(
                sdp="", type="", success=False, error=str(e)
            )
    
    async def _setup_connection_handlers(self, pc: RTCPeerConnection, session_id: int):
        """Настройка обработчиков событий соединения"""
        
        async def remove_client_by_timeout():
            logger.info(f">> WebRTC timeout killer for session {session_id}")
            await asyncio.sleep(float(settings.uninitialized_rtc_kill_timeout))
            logger.info(f"<< WebRTC timeout closing session {session_id}")
            await pc.close()

        killer_task = asyncio.create_task(remove_client_by_timeout())

        @pc.on("connectionstatechange")
        async def on_connection_state_change():
            if pc.connectionState == "connected":
                if not killer_task.cancelled() and not killer_task.done():
                    killer_task.cancel()
                logger.info(f"QUEUES STATE CONN: AUDIO_SCQ={AUDIO_SECOND_QUEUE.qsize()}, SENTENCES_QUEUE={SENTENCES_QUEUE.qsize()}")
                try:
                    await asyncio.wait_for(RTC_STREAM_CONNECTED.acquire(), 60)
                    logger.info(f"🎵 WebRTC peer connected {session_id}")
                    logger.info("🎵 CAN_SEND_FRAMES.set()")
                    CAN_SEND_FRAMES.set()
                    STATE.current_session_id = session_id
                    STATE.current_pc = pc
                    STATE.force_flush_queues()
                    asyncio.run_coroutine_threadsafe(start_synthesize_worker(), self.main_loop)
                except asyncio.TimeoutError:
                    logger.info(f"🔒 WebRTC peer tried to connect to locked resource {session_id}")
                    await pc.close()

            elif pc.connectionState in ("failed", "disconnected", "closed"):
                if not killer_task.cancelled() and not killer_task.done():
                    killer_task.cancel()
                logger.info(f"🎵 WebRTC peer disconnected {session_id} csid={STATE.current_session_id} (state={pc.connectionState})")
                if session_id == STATE.current_session_id:
                    logger.info("🎵 CAN_SEND_FRAMES.clear()")
                    CAN_SEND_FRAMES.clear()
                    if not WS_CONTROL_CONNECTED.locked():
                        AVATAR_SET.clear()
                    STATE.auto_idle = True
                    try:
                        logger.info(f"RTC_STREAM_CONNECTED.release()")
                        RTC_STREAM_CONNECTED.release()
                        STATE.current_pc = None
                        STATE.current_session_id = None
                    except ValueError as e:
                        logger.error(f"RTC_STREAM_CONNECTED.release() -> {e!r}")
                    stop_result = asyncio.run_coroutine_threadsafe(stop_synthesize_worker(), self.main_loop)
                    await asyncio.wrap_future(stop_result)
                    STATE.kill_streamer()

                await pc.close()

    async def process_offer_async(self, offer_data: Dict[str, Any], turn: bool = False) -> Dict[str, Any]:
        """Публичный метод для обработки offer из FastAPI потока"""
        if not self.running or not self.webrtc_loop:
            raise RuntimeError("WebRTC thread not running")
        
        # Создаем запрос
        session_id = int(time.time() * 1000) % 1000000  # simple session ID
        request = OfferRequest(
            sdp=offer_data["sdp"],
            type=offer_data["type"], 
            session_id=session_id,
            turn=turn
        )
        
        # Создаем Future для результата
        future = Future()
        
        # Отправляем запрос в WebRTC поток
        self.webrtc_loop.call_soon_threadsafe(
            self.request_queue.put_nowait, (request, future)
        )
        
        # Ждем результат (с таймаутом)
        response = future.result(timeout=10.0)
        
        if not response.success:
            raise RuntimeError(f"WebRTC offer processing failed: {response.error}")
        
        return {
            "sdp": response.sdp,
            "type": response.type,
        }

# Глобальный менеджер
webrtc_manager = WebRTCManager()
