"""
Common logging configuration for all uvicorn-based services.

Design goals:
- Universal base setup usable by any service (no app-specific logic)
- Consistent timestamps and levels across apps
- Optional, opt-in helpers to extend behavior (e.g., access log filtering)
"""
import logging
import re
from typing import Dict, Any, List, Optional
from apps.common.helpers import env_str, env_bool


def setup_logging():
    """Configure application logging with timestamps."""
    log_level = (env_str("LOG_LEVEL", "INFO") or "INFO").upper()
    
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        # add milliseconds to timestamps for finer sequencing
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True
    )

class RegexAccessFilter(logging.Filter):
    """Filter to suppress uvicorn.access lines that match any regex pattern.

    Intended for simple suppression like GET /render/{job_id} while keeping
    other access logs. Patterns are compiled on init.
    """
    def __init__(self, patterns: Optional[List[str]] = None) -> None:
        super().__init__()
        self._patterns: List[re.Pattern[str]] = []
        for p in patterns or []:
            try:
                self._patterns.append(re.compile(p))
            except re.error:
                # Skip invalid patterns silently to avoid breaking logging
                continue

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for rp in self._patterns:
            try:
                if rp.search(msg):
                    return False
            except Exception:
                # Fail-open if a pattern misbehaves
                continue
        return True


class SanitizeAccessQueryFilter(logging.Filter):
    """Strip query strings from uvicorn access log request lines.

    Example transformation:
      "GET /jobs/123?expires=900 HTTP/1.1" -> "GET /jobs/123 HTTP/1.1"
    """

    _request_line_re = re.compile(r'"([A-Z]+)\s+([^"\s]+)\s+(HTTP/[0-9.]+)"')

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True

        def _replace(match: re.Match[str]) -> str:
            method, target, version = match.groups()
            safe_target = target.split("?", 1)[0]
            return f'"{method} {safe_target} {version}"'

        safe_msg = self._request_line_re.sub(_replace, msg, count=1)
        if safe_msg != msg:
            # Freeze sanitized message so formatter won't re-expand with old args.
            record.msg = safe_msg
            record.args = ()
        return True


def get_uvicorn_log_config(
    *,
    suppress_access_regexes: Optional[List[str]] = None,
    strip_query_strings: Optional[bool] = None,
) -> Dict[str, Any]:
    """Return uvicorn log config with timestamps for all handlers.

    Parameters:
    - suppress_access_regexes: when provided, adds a filter to uvicorn.access that
      suppresses log lines whose message matches any of the given regex patterns.
      Example: r'"GET /render/(?!start)' to suppress poll GETs but keep /render/start.
    - strip_query_strings: when True, strips `?query=...` from access log request
      lines to avoid leaking sensitive query parameters.
    """
    log_level = (env_str("LOG_LEVEL", "INFO") or "INFO").upper()
    if strip_query_strings is None:
        strip_query_strings = env_bool("ACCESS_LOG_STRIP_QUERY", True)

    filters: Dict[str, Any] = {}
    access_handler: Dict[str, Any] = {
        "formatter": "access",
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stdout",
    }
    access_filters: List[str] = []

    if strip_query_strings:
        filters["strip_access_query"] = {
            "()": "apps.common.logging_config.SanitizeAccessQueryFilter",
        }
        access_filters.append("strip_access_query")

    if suppress_access_regexes:
        filters["suppress_access_regexes"] = {
            "()": "apps.common.logging_config.RegexAccessFilter",
            "patterns": list(suppress_access_regexes),
        }
        access_filters.append("suppress_access_regexes")

    if access_filters:
        access_handler["filters"] = access_filters

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                # add milliseconds to timestamps
                "format": "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "access": {
                # add milliseconds to timestamps
                "format": "%(asctime)s.%(msecs)03d [%(levelname)s] uvicorn.access: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": access_handler,
        },
        "filters": filters,
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": log_level, "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": log_level, "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
        "root": {
            "level": log_level,
            "handlers": ["default"]
        },
    }
