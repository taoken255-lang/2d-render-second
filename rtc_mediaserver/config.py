"""Configuration settings for RTC Media Server."""

from pathlib import Path
from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

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
    grpc_server_url: str = "localhost:8501"
    grpc_secure_channel: bool = False

    # Audio settings
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    audio_format: str = "s16le"
    audio_chunk_duration: float = 1.0

    # Video settings
    video_width: int = 640
    video_height: int = 480
    video_fps: int = 30

    # Logging settings
    log_level: str = "INFO"
    log_format: str = "text"
    log_text_format: str = (
        "%(asctime)s.%(msecs)03d - %(threadName)s - %(levelname)s - "
        "%(name)s - %(filename)s:%(funcName)s:%(lineno)d - %(message)s"
    )
    log_date_format: str = "%Y-%m-%d %H:%M:%S"
    log_json_ensure_ascii: bool = False
    log_file: Optional[Path] = None
    third_party_log_level: str = "WARNING"

    uninitialized_rtc_kill_timeout: int = 60
    max_inflight_chunks: int = 3
    sync_queue_size: int = 3

    debug_page_enabled: bool = False

    elevenlabs_api_url: str = "https://karimrashid.metahumansdk.io/"
    elevenlabs_ws_url: str = "wss://karimrashid.metahumansdk.io/"
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    elevenlabs_model_id: str = "eleven_flash_v2_5"
    elevenlabs_optimize: int = 1
    elevenlabs_voice_speed: float = 1.1
    elevenlabs_type: str = "http"
    elevenlabs_stability: float = 0.87
    elevenlabs_default_lang: Optional[str] = None

    bitrate: int = 16_000_000

    offline_output_path: Path = Path("offline_data")
    offline_tail_videos_path: Path = Path("samples")
    ffmpeg_debug_enabled: bool = False
    offline_results_to_keep: int = 10
    offline_avatars: List[str] = ["iirina", "iirina_acquisition"]
    offline_warmup: bool = False

    turn_enabled: bool = False
    turn_server: str = "87.242.91.109:19303"
    turn_login: str = "iiTh7jijiemu"
    turn_password: str = "aoGheibiaz5u"

    fast_interrupts_enabled: bool = False

    ffmpeg_debug: bool = False
    watchdog_enabled: bool = True

    healthcheck_enabled: bool = True

    @field_validator("log_level", "third_party_log_level", mode="before")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        return str(value).upper()

    @field_validator("log_format", mode="before")
    @classmethod
    def validate_log_format(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"text", "json"}:
            raise ValueError("log_format must be either 'text' or 'json'")
        return normalized


settings = Settings()
