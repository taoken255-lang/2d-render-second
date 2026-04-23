"""Logging configuration for RTC Media Server."""

import logging
import sys
import threading
from pathlib import Path
from typing import Optional

from pythonjsonlogger.jsonlogger import JsonFormatter

from .config import Settings, settings

_TEXT_FORMAT = "text"
_JSON_FORMAT = "json"
_LOGGING_LOCK = threading.Lock()
_LOGGING_CONFIGURED = False


def setup_logging(
    level: Optional[str] = None,
    format_string: Optional[str] = None,
    log_file: Optional[str | Path] = None,
    log_format: Optional[str] = None,
    force: bool = False,
) -> None:
    """Setup process-wide logging before application startup."""

    global _LOGGING_CONFIGURED

    with _LOGGING_LOCK:
        if _LOGGING_CONFIGURED and not force:
            return

        configured_level = (level or settings.log_level).upper()
        configured_format = (log_format or settings.log_format).lower()
        configured_format_string = format_string or settings.log_text_format
        configured_log_file = log_file if log_file is not None else settings.log_file

        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.setLevel(_resolve_level(configured_level))

        for handler in _create_handlers(
            log_format=configured_format,
            format_string=configured_format_string,
            log_file=configured_log_file,
            active_settings=settings,
        ):
            root_logger.addHandler(handler)

        _configure_specific_loggers(settings)
        _LOGGING_CONFIGURED = True


def _create_handlers(
    *,
    log_format: str,
    format_string: str,
    log_file: Optional[str | Path],
    active_settings: Settings,
) -> list[logging.Handler]:
    handlers: list[logging.Handler] = []

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(_resolve_level(active_settings.log_level))
    console_handler.setFormatter(
        _build_formatter(log_format, format_string, active_settings)
    )
    handlers.append(console_handler)

    if log_file:
        file_path = Path(log_file)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(file_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            _build_formatter(log_format, format_string, active_settings)
        )
        handlers.append(file_handler)

    return handlers


def _build_formatter(
    log_format: str,
    format_string: str,
    active_settings: Settings,
) -> logging.Formatter:
    if log_format == _JSON_FORMAT:
        return JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(threadName)s "
            "%(filename)s %(funcName)s %(lineno)d %(message)s",
            datefmt=active_settings.log_date_format,
            json_ensure_ascii=active_settings.log_json_ensure_ascii,
        )
    if log_format != _TEXT_FORMAT:
        raise ValueError(f"Unsupported log format: {log_format}")
    return logging.Formatter(
        fmt=format_string,
        datefmt=active_settings.log_date_format,
    )


def _resolve_level(level: str) -> int:
    resolved_level = getattr(logging, level.upper(), None)
    if not isinstance(resolved_level, int):
        raise ValueError(f"Unsupported log level: {level}")
    return resolved_level


def _configure_specific_loggers(active_settings: Settings) -> None:
    third_party_level = _resolve_level(active_settings.third_party_log_level)
    app_level = _resolve_level(active_settings.log_level)

    logging.getLogger("uvicorn.access").setLevel(third_party_level)
    logging.getLogger("uvicorn.error").setLevel(third_party_level)
    logging.getLogger("websockets").setLevel(third_party_level)
    logging.getLogger("aiortc").setLevel(third_party_level)
    logging.getLogger("av").setLevel(third_party_level)
    logging.getLogger("grpc").setLevel(third_party_level)
    logging.getLogger("rtc_mediaserver").setLevel(app_level)


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def setup_default_logging() -> None:
    setup_logging(force=True)
