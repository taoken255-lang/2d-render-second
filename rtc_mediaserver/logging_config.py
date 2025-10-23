"""Logging configuration for RTC Media Server."""

import logging
import sys
from typing import Optional

from .config import settings


def setup_logging(
    level: str = "DEBUG",
    format_string: Optional[str] = None,
    log_file: Optional[str] = None
) -> None:
    """Setup logging configuration.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format_string: Custom format string for log messages
        log_file: Optional file path for logging to file
    """
    # Default format if not provided - включаем информацию о потоке
    if format_string is None:
        format_string = '%(asctime)s.%(msecs)03d - %(threadName)s - %(levelname)s - %(filename)s:%(funcName)s:%(lineno)d - %(message)s'
    
    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=format_string,
        handlers=_create_handlers(log_file)
    )
    
    # Set specific loggers
    _configure_specific_loggers()


def _create_handlers(log_file: Optional[str] = None) -> list:
    """Create logging handlers."""
    handlers = []
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    handlers.append(console_handler)
    
    # File handler (if log_file is specified)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        handlers.append(file_handler)
    
    return handlers


def _configure_specific_loggers() -> None:
    """Configure specific loggers with custom levels."""
    # Reduce noise from external libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiortc").setLevel(logging.INFO)
    logging.getLogger("av").setLevel(logging.WARNING)
    logging.getLogger("grpc").setLevel(logging.WARNING)
    
    # Set our application loggers to DEBUG for development
    logging.getLogger("rtc_mediaserver").setLevel(logging.DEBUG)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the specified name.
    
    Args:
        name: Logger name (usually __name__)
        
    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


# Convenience function for quick setup
def setup_default_logging() -> None:
    """Setup default logging configuration."""
    setup_logging(
        level="DEBUG",
        format_string='%(asctime)s - %(threadName)s - %(levelname)s - %(filename)s:%(funcName)s:%(lineno)d - %(message)s'
    ) 