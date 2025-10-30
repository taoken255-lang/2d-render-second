"""Configuration settings for RTC Media Server."""

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings."""
    
    # Server settings
    host: str = "0.0.0.0"
    port: int = 8080
    https: bool = False
    ssl_cert: str = ""
    ssl_key: str = ""
    
    # WebSocket settings
    websocket_host: str = "0.0.0.0"
    websocket_port: int = 8001
    
    # gRPC settings
    grpc_server_url: str = "localhost:8501" #"2d.digitalavatars.ru"  # Default 2d-render port
    grpc_secure_channel: bool = False

    # Audio settings
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    audio_format: str = "s16le"
    audio_chunk_duration: float = 1.0  # Duration of audio chunks sent to 2d-render (seconds)
    
    # Video settings
    video_width: int = 640
    video_height: int = 480
    video_fps: int = 30
    
    # Logging settings
    log_level: str = "DEBUG"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    uninitialized_rtc_kill_timeout: int = 60
    max_inflight_chunks: int = 2

    debug_page_enabled: bool = True

    elevenlabs_api_url: str = "https://karimrashid.metahumansdk.io/" #"https://api.elevenlabs.io/"
    elevenlabs_ws_url: str = "wss://karimrashid.metahumansdk.io/"
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    elevenlabs_model_id: str = "eleven_flash_v2_5"
    elevenlabs_optimize: int = 1
    elevenlabs_voice_speed: float = 1.1
    elevenlabs_type: str = "http"
    elevenlabs_stability: float = 0.87

    bitrate: int = 16_000_000

    offline_output_path: Path = Path("offline_data")

    fast_interrupts_enabled: bool = False

# Global settings instance
settings = Settings() 