"""
Common logging configuration for all services (uvicorn and long-running).

Design goals:
- Single source of truth for app log formatting and LOG_FORMAT env parsing.
- One consistent format across services; switch to JSON via LOG_FORMAT=json.
- Dot-millisecond timestamps in plain logs; Unix timestamps in JSON logs.
- Opt-in helpers to extend uvicorn behavior (e.g., access log filtering).
"""
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import os
import re
import sys
from typing import Dict, Any, List, Optional
from apps.common.helpers import env_str, env_bool, env_int

try:
    # python-json-logger v3+
    from pythonjsonlogger.json import JsonFormatter
except ImportError:
    # python-json-logger v2.x fallback
    from pythonjsonlogger import jsonlogger
    JsonFormatter = jsonlogger.JsonFormatter


_PLAIN_FMT = "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s"
_ACCESS_PLAIN_FMT = "%(asctime)s.%(msecs)03d [%(levelname)s] uvicorn.access: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# JSON formatter internals use stdlib LogRecord field names; JSON keys are
# renamed on emit via rename_fields so kontur sees timestamp/level/logger.
#
# request_id is not in the format string: putting it there would make
# JsonFormatter overwrite our default with record.__dict__.get('request_id')
# (i.e., None) when no extra is supplied. Instead it lives in defaults and
# gets overridden by extra={"request_id": ...} via merge_record_extra.
#
# exception: JsonFormatter auto-emits `exc_info` when record.exc_info is set;
# rename it to `exception` so kontur sees a single consistent field name.
_JSON_FMT = "%(levelname)s %(name)s %(message)s"
_JSON_RENAME = {
    "levelname": "level",
    "name": "logger",
    "exc_info": "exception",
}
_JSON_DEFAULTS = {
    "request_id": "-",
    "job_id": "-",
    "trace_id": "-",
    "span_id": "-",
}
_JSON_TIMESTAMP_FORMATS = {
    "float_seconds",
    "int_seconds",
    "int_milliseconds",
    "int_microseconds",
}

_LOG_FORMAT_WARNING_EMITTED = False
_LOG_JSON_TIMESTAMP_FORMAT_WARNING_EMITTED = False
_RESOLVED_LOG_FORMAT: Optional[str] = None
_RESOLVED_JSON_TIMESTAMP_FORMAT: Optional[str] = None
_RESOLVED_SERVICE_NAME: Optional[str] = None


def _normalize_log_field(value: Any) -> str:
    """Normalize optional correlation fields for JSON logs."""
    if value is None:
        return "-"
    text = str(value).strip()
    if not text:
        return "-"
    if text.lower() in {"dummy", "none", "null", "undefined"}:
        return "-"
    return text


def _resolve_service_name() -> str:
    """Return explicit service name when configured, else a minimal fallback."""
    if _RESOLVED_SERVICE_NAME:
        return _RESOLVED_SERVICE_NAME
    program = Path(os.path.basename(sys.argv[0] or "")).stem
    normalized = _normalize_log_field(program)
    return normalized if normalized != "-" else "app"


class UnixTimestampJsonFormatter(JsonFormatter):
    """JsonFormatter variant that emits `timestamp` as Unix seconds."""

    def __init__(self, *args: Any, service_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._service_name = service_name

    def add_fields(
        self,
        log_record: Dict[str, Any],
        record: logging.LogRecord,
        message_dict: Dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        ts_format = _RESOLVED_JSON_TIMESTAMP_FORMAT or get_json_timestamp_format()
        if ts_format == "int_seconds":
            timestamp = int(record.created)
        elif ts_format == "int_milliseconds":
            timestamp = int(record.created * 1000)
        elif ts_format == "int_microseconds":
            timestamp = int(record.created * 1_000_000)
        else:
            timestamp = record.created
        log_record["timestamp"] = timestamp
        log_record["service"] = self._service_name
        log_record["job_id"] = _normalize_log_field(log_record.get("job_id"))
        log_record["trace_id"] = _normalize_log_field(log_record.get("trace_id"))
        log_record["span_id"] = _normalize_log_field(log_record.get("span_id"))
        log_record.pop("asctime", None)


def get_log_format() -> str:
    """Resolve LOG_FORMAT env: 'plain' (default) or 'json'.

    Pure resolver — does not emit any log records. The bad-value warning is
    deferred to warn_invalid_log_format_once() so it only fires after
    setup_logging() has installed handlers; otherwise the warning would come
    out via Python's auto-configured root logger in the wrong format.

    Callers outside this module must not re-parse LOG_FORMAT; route through here.
    """
    value = (env_str("LOG_FORMAT", "plain") or "plain").strip().lower()
    if value in {"plain", "json"}:
        return value
    return "plain"


def warn_invalid_log_format_once() -> None:
    """Emit the bad-LOG_FORMAT warning at most once per process.

    Must be called AFTER setup_logging() has configured handlers, so the
    warning comes out in the unified plain/JSON format. get_uvicorn_log_config()
    must not call this — uvicorn reads the already-resolved mode.
    """
    global _LOG_FORMAT_WARNING_EMITTED
    if _LOG_FORMAT_WARNING_EMITTED:
        return
    value = (env_str("LOG_FORMAT", "plain") or "plain").strip().lower()
    if value in {"plain", "json"}:
        return
    logging.getLogger(__name__).warning(
        "Invalid LOG_FORMAT=%r, falling back to 'plain'", value,
    )
    _LOG_FORMAT_WARNING_EMITTED = True


def get_json_timestamp_format() -> str:
    """Resolve JSON timestamp format env with fallback.

    Supported values:
    - float_seconds
    - int_seconds
    - int_milliseconds
    - int_microseconds
    """
    value = (
        env_str("LOG_JSON_TIMESTAMP_FORMAT", "float_seconds") or "float_seconds"
    ).strip().lower()
    if value in _JSON_TIMESTAMP_FORMATS:
        return value
    return "float_seconds"


def warn_invalid_json_timestamp_format_once() -> None:
    """Emit the bad LOG_JSON_TIMESTAMP_FORMAT warning at most once per process."""
    global _LOG_JSON_TIMESTAMP_FORMAT_WARNING_EMITTED
    if _LOG_JSON_TIMESTAMP_FORMAT_WARNING_EMITTED:
        return
    value = (
        env_str("LOG_JSON_TIMESTAMP_FORMAT", "float_seconds") or "float_seconds"
    ).strip().lower()
    if value in _JSON_TIMESTAMP_FORMATS:
        return
    logging.getLogger(__name__).warning(
        "Invalid LOG_JSON_TIMESTAMP_FORMAT=%r, falling back to 'float_seconds'",
        value,
    )
    _LOG_JSON_TIMESTAMP_FORMAT_WARNING_EMITTED = True


def build_json_formatter() -> logging.Formatter:
    """Build JsonFormatter with kontur-aligned field names and Unix timestamps.

    Public so logging.config.dictConfig can instantiate it via
    {"()": "apps.common.logging_config.build_json_formatter"}.

    JSON logs use LogRecord.created and are shaped by
    LOG_JSON_TIMESTAMP_FORMAT.
    """
    service_name = _RESOLVED_SERVICE_NAME or _resolve_service_name()
    fmt = UnixTimestampJsonFormatter(
        _JSON_FMT,
        service_name=service_name,
        rename_fields=_JSON_RENAME,
        defaults=_JSON_DEFAULTS,
    )
    return fmt


def _get_file_log_path() -> Path:
    log_dir = Path(env_str("LOG_FILE_DIR", "logs") or "logs")
    service_name = _RESOLVED_SERVICE_NAME or _resolve_service_name()
    log_name = f"{service_name}.log.json"
    return log_dir / log_name


def _build_file_handler(level: int) -> TimedRotatingFileHandler:
    log_path = _get_file_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    backup_count = max(0, env_int("LOG_FILE_BACKUP_COUNT", 7))
    handler = TimedRotatingFileHandler(
        filename=str(log_path),
        when="midnight",
        interval=1,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    return handler


def _file_logging_enabled() -> bool:
    return env_bool("LOG_FILE_ENABLED", False)


def _file_handler_config() -> Dict[str, Any]:
    log_path = _get_file_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "class": "logging.handlers.TimedRotatingFileHandler",
        "filename": str(log_path),
        "when": "midnight",
        "interval": 1,
        "backupCount": max(0, env_int("LOG_FILE_BACKUP_COUNT", 7)),
        "encoding": "utf-8",
    }


def setup_logging(*, service_name: Optional[str] = None):
    """Configure root logging per LOG_LEVEL and LOG_FORMAT.

    Both modes reset root handlers via force=True so any handlers created
    before this call (e.g. by a stray logging.warning during imports)
    don't survive alongside the configured one.
    """
    global _RESOLVED_LOG_FORMAT, _RESOLVED_JSON_TIMESTAMP_FORMAT, _RESOLVED_SERVICE_NAME
    log_level = (env_str("LOG_LEVEL", "INFO") or "INFO").upper()
    _RESOLVED_LOG_FORMAT = get_log_format()
    _RESOLVED_JSON_TIMESTAMP_FORMAT = get_json_timestamp_format()
    if service_name is not None:
        _RESOLVED_SERVICE_NAME = _normalize_log_field(service_name)
    else:
        _RESOLVED_SERVICE_NAME = _resolve_service_name()
    level = getattr(logging, log_level, logging.INFO)
    handlers: List[logging.Handler]

    if _RESOLVED_LOG_FORMAT == "json":
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(build_json_formatter())
        handlers = [stream_handler]
    else:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter(_PLAIN_FMT, datefmt=_DATEFMT))
        handlers = [stream_handler]

    if _file_logging_enabled():
        file_handler = _build_file_handler(level)
        if _RESOLVED_LOG_FORMAT == "json":
            file_handler.setFormatter(build_json_formatter())
        else:
            file_handler.setFormatter(logging.Formatter(_PLAIN_FMT, datefmt=_DATEFMT))
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers, force=True)

    # Emit the bad-LOG_FORMAT warning (if any) AFTER handlers are installed,
    # so it comes out in the unified format, not via Python's default root config.
    warn_invalid_log_format_once()
    warn_invalid_json_timestamp_format_once()


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
    """Return uvicorn log config honoring the shared LOG_FORMAT switch.

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

    if get_log_format() == "json":
        formatters = {
            "default": {"()": "apps.common.logging_config.build_json_formatter"},
            "access": {"()": "apps.common.logging_config.build_json_formatter"},
        }
    else:
        formatters = {
            "default": {
                "format": _PLAIN_FMT,
                "datefmt": _DATEFMT,
            },
            "access": {
                "format": _ACCESS_PLAIN_FMT,
                "datefmt": _DATEFMT,
            },
        }

    handlers: Dict[str, Any] = {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": access_handler,
    }

    default_handler_names = ["default"]
    access_handler_names = ["access"]

    if _file_logging_enabled():
        handlers["file"] = {
            **_file_handler_config(),
            "formatter": "default",
        }
        default_handler_names.append("file")
        access_handler_names.append("file")

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": formatters,
        "handlers": handlers,
        "filters": filters,
        "loggers": {
            "uvicorn": {"handlers": default_handler_names, "level": log_level, "propagate": False},
            "uvicorn.error": {"handlers": default_handler_names, "level": log_level, "propagate": False},
            "uvicorn.access": {"handlers": access_handler_names, "level": "INFO", "propagate": False},
        },
        "root": {
            "level": log_level,
            "handlers": default_handler_names
        },
    }
