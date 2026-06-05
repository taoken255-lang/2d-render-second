import logging
import os

from apps.common.logging_config import setup_logging


class _LoguruToStdlibSink:
    """Forward legacy loguru records into the shared stdlib logging config."""

    def __call__(self, message) -> None:
        record = message.record
        exception = record["exception"]
        exc_info = None
        if exception is not None:
            exc_info = (exception.type, exception.value, exception.traceback)

        log_record = logging.LogRecord(
            name=record["name"],
            level=record["level"].no,
            pathname=record["file"].path,
            lineno=record["line"],
            msg=record["message"],
            args=(),
            exc_info=exc_info,
            func=record["function"],
            sinfo=None,
        )
        log_record.created = record["time"].timestamp()
        log_record.msecs = int(record["time"].microsecond / 1000)

        for key, value in record["extra"].items():
            setattr(log_record, key, value)

        logging.getLogger(record["name"]).handle(log_record)


def _install_loguru_bridge(level: int) -> None:
    try:
        from loguru import logger
    except ImportError:
        return

    logger.remove()
    logger.configure(
        extra={
            "request_id": "-",
            "job_id": "-",
            "trace_id": "-",
            "span_id": "-",
        }
    )
    logger.add(
        _LoguruToStdlibSink(),
        level=level,
        backtrace=True,
        diagnose=False,
        format="{message}",
    )


def configure_logging(level: str | None = None, log_format: str | None = None) -> None:
    """Configure legacy services through apps.common.logging_config."""
    if level is not None:
        os.environ["LOG_LEVEL"] = level
    if log_format is not None:
        os.environ["LOG_FORMAT"] = log_format

    setup_logging()
    resolved_level = logging.getLogger().level
    _install_loguru_bridge(resolved_level)
