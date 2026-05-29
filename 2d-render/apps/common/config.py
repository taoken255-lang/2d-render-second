"""
Shared configuration loader with file-first lookup and env fallback.

Lookup order:
1) render config file (`RENDER_CONFIG_FILE`, default `/run/async_render/config`)
2) environment variable
3) provided default argument
4) `None` when no default was provided

Public API mirrors `os.getenv` ergonomics:
    config.get("KEY", "default")
    config.get("KEY")          # optional, returns None when missing
    config.require("KEY")      # required, raises when missing
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Dict

log = logging.getLogger("common.config")

RENDER_CONFIG_FILE_ENV = "RENDER_CONFIG_FILE"
DEFAULT_RENDER_CONFIG_FILE = "/run/async_render/config"

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_lock = threading.Lock()
_cached_path: str | None = None
_cached_mtime_ns: int | None = None
_cached_values: Dict[str, str] = {}
_warned_missing_paths: set[str] = set()
_last_load_state: str = "unknown"
_startup_status_logged = False


def _resolve_path() -> str:
    return os.getenv(RENDER_CONFIG_FILE_ENV, DEFAULT_RENDER_CONFIG_FILE)


def _config_file_explicitly_configured() -> bool:
    """
    True when operator explicitly requested file-based config lookup.

    In env-only deployments (no RENDER_CONFIG_FILE set), missing/unreadable
    default path is expected and should not be logged as warning noise.
    """
    return os.getenv(RENDER_CONFIG_FILE_ENV) is not None


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_config_file(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                log.warning("config: ignoring malformed line path=%s line=%d", path, line_no)
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not _KEY_RE.match(key):
                log.warning(
                    "config: ignoring invalid key path=%s line=%d key=%r",
                    path,
                    line_no,
                    key,
                )
                continue
            values[key] = _strip_wrapping_quotes(value.strip())
    return values


def _load_values() -> Dict[str, str]:
    global _cached_path, _cached_mtime_ns, _cached_values, _last_load_state

    path = _resolve_path()

    with _lock:
        try:
            stat = os.stat(path)
            mtime_ns = stat.st_mtime_ns
        except FileNotFoundError:
            if path not in _warned_missing_paths:
                if _config_file_explicitly_configured():
                    log.warning("config: file not found path=%s; using env/default", path)
                else:
                    log.info(
                        "config: file not found path=%s; using env/default (expected in env-only mode)",
                        path,
                    )
                _warned_missing_paths.add(path)
            _cached_path = path
            _cached_mtime_ns = None
            _cached_values = {}
            _last_load_state = "env-default:file-not-found"
            return _cached_values
        except OSError as exc:
            if _config_file_explicitly_configured():
                log.warning("config: cannot stat file path=%s error=%s; using env/default", path, exc)
            else:
                log.info(
                    "config: cannot stat file path=%s error=%s; using env/default (expected in env-only mode)",
                    path,
                    exc,
                )
            _cached_path = path
            _cached_mtime_ns = None
            _cached_values = {}
            _last_load_state = "env-default:stat-error"
            return _cached_values

        if _cached_path == path and _cached_mtime_ns == mtime_ns:
            return _cached_values

        try:
            parsed = _parse_config_file(path)
        except OSError as exc:
            if _config_file_explicitly_configured():
                log.warning("config: cannot read file path=%s error=%s; using env/default", path, exc)
            else:
                log.info(
                    "config: cannot read file path=%s error=%s; using env/default (expected in env-only mode)",
                    path,
                    exc,
                )
            _cached_path = path
            _cached_mtime_ns = mtime_ns
            _cached_values = {}
            _last_load_state = "env-default:read-error"
            return _cached_values

        _cached_path = path
        _cached_mtime_ns = mtime_ns
        _cached_values = parsed
        _last_load_state = "file"
        log.info("config: loaded file path=%s keys=%d", path, len(parsed))
        return _cached_values


def get(name: str, default: Any = None) -> Any:
    """
    Get config value by key.

    - `get("KEY", "default")` -> file/env/default
    - `get("KEY")` -> file/env/None
    """
    if not _KEY_RE.match(name):
        raise ValueError(f"Invalid config key {name!r}; expected pattern {_KEY_RE.pattern}")

    values = _load_values()
    if name in values:
        log.debug("config: key=%s source=file", name)
        return values[name]

    env_val = os.getenv(name)
    if env_val is not None:
        log.debug("config: key=%s source=env", name)
        return env_val

    log.debug("config: key=%s source=default", name)
    return default


def require(name: str) -> str:
    """Get a required config value or raise RuntimeError when missing/empty."""
    val = get(name)
    if val is None or (isinstance(val, str) and val == ""):
        log.error("config: missing required key=%s source=missing", name)
        raise RuntimeError(f"Missing required config key: {name}")
    return str(val)


def log_startup_status() -> None:
    """
    Emit one startup log with config source status (file or env/default fallback).

    Call this after logging is configured in the service entrypoint.
    """
    global _startup_status_logged
    with _lock:
        if _startup_status_logged:
            return
        _startup_status_logged = True

    values = _load_values()
    path = _resolve_path()
    state = _last_load_state
    if state == "file":
        log.info("config: startup source=file path=%s keys=%d", path, len(values))
    else:
        log.info("config: startup source=env/default path=%s reason=%s", path, state)


def reset_cache() -> None:
    """Reset cached file state (tests/debug utilities)."""
    global _cached_path, _cached_mtime_ns, _cached_values, _last_load_state, _startup_status_logged
    with _lock:
        _cached_path = None
        _cached_mtime_ns = None
        _cached_values = {}
        _last_load_state = "unknown"
        _startup_status_logged = False
        _warned_missing_paths.clear()
