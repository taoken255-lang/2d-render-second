import logging
import os
import sys
import traceback

from loguru import logger


TEXT_LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} [{level}] {name}: {message}"
LOG_FORMAT_ENV = "LOG_FORMAT"
JSON_LOG_FORMAT = "%(timestamp)s %(level)s %(logger)s %(message)s %(request_id)s %(exception)s"


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False

    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_log_format(log_format: str | None) -> str:
    resolved_log_format = (log_format or os.getenv(LOG_FORMAT_ENV) or "").strip().lower()
    if resolved_log_format == "json":
        return "json"

    if resolved_log_format in {"", "text", "plain", "console"}:
        return "text"

    if _is_truthy(resolved_log_format):
        return "json"

    return "text"


def _build_json_formatter():
    try:
        from pythonjsonlogger.json import JsonFormatter
    except ImportError:
        from pythonjsonlogger import jsonlogger

        JsonFormatter = jsonlogger.JsonFormatter

    return JsonFormatter(JSON_LOG_FORMAT)


class JsonLogSink:
    def __init__(self, stream) -> None:
        self.stream = stream
        self.formatter = _build_json_formatter()

    def __call__(self, message) -> None:
        record = message.record
        exception = record["exception"]
        log_record = logging.LogRecord(
            name=record["name"],
            level=record["level"].no,
            pathname=record["file"].path,
            lineno=record["line"],
            msg=record["message"],
            args=(),
            exc_info=None,
            func=record["function"],
            sinfo=None,
        )

        log_record.timestamp = record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_record.level = record["level"].name
        log_record.logger = record["name"]
        log_record.request_id = record["extra"].get("request_id", "-")
        log_record.exception = None

        if exception is not None:
            log_record.exception = "".join(
                traceback.format_exception(
                    exception.type,
                    exception.value,
                    exception.traceback,
                )
            ).strip()

        for key, value in record["extra"].items():
            setattr(log_record, key, value)

        self.stream.write(f"{self.formatter.format(log_record)}\n")
        self.stream.flush()


def configure_logging(level: str | None = None, log_format: str | None = None) -> None:
    resolved_level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    resolved_log_format = _resolve_log_format(log_format)

    logger.configure(extra={"request_id": "-"})
    logger.remove()
    if resolved_log_format == "json":
        logger.add(
            JsonLogSink(sys.stdout),
            level=resolved_level,
            backtrace=True,
            diagnose=False,
        )
        return

    logger.add(
        sys.stdout,
        format=TEXT_LOG_FORMAT,
        level=resolved_level,
        colorize=False,
        backtrace=True,
        diagnose=False,
    )
