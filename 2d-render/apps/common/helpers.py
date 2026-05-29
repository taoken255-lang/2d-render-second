"""
Helper utilities shared across apps.

Provides:
- now_iso(): current UTC ISO timestamp
- env_* helpers: consistent parsing of environment variables
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Callable
from pathlib import Path
import asyncio
import logging
import fnmatch

from apps.common import config

def now_iso() -> str:
    """Return current UTC timestamp in ISO format (seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read an environment variable as string with a default."""
    val = config.get(name, default)
    return val if val is not None else default


def env_int(name: str, default: int) -> int:
    """Read an environment variable as int with a default (robust parse)."""
    val = config.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    """Read an environment variable as float with a default (robust parse)."""
    val = config.get(name)
    if val is None:
        return default
    try:
        return float(val)
    except Exception:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    """Read an environment variable as bool.

    Truthy: 1, true, t, yes, y, on
    Falsy:  0, false, f, no, n, off, ""
    Case-insensitive. Falls back to default when unset.
    """
    val = config.get(name)
    if val is None:
        return default
    s = str(val).strip().lower()
    # Numeric values: treat any non-zero as True
    try:
        if s not in ("",):
            num = float(s)
            return num != 0.0
    except Exception:
        pass
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off", ""):
        return False
    # Non-standard value: fall back to default
    return default


def require_env(name: str) -> str:
    """Get an environment variable or raise RuntimeError if missing/empty.

    Logs critical error before raising to ensure visibility in container logs.
    """
    return config.require(name)


async def stream_subprocess(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    log_path: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
    prefix: str = "",
    on_line: Optional[Callable[[str], None]] = None,
    ts_prefix: bool = False,
    cancel_event: Optional[asyncio.Event] = None,
    stdin_data: Optional[bytes] = None,
) -> int:
    """
    Run a subprocess, streaming merged stdout/stderr line-by-line.

    - Writes lines to `log_path` when provided
    - Logs lines with optional `prefix` when logger provided
    - Calls `on_line(text)` for each decoded line (no newline)
    - Optionally writes `stdin_data` to subprocess stdin
    - Returns the process return code
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(cwd) if cwd else None,
        start_new_session=True,  # Create new process group to kill all children
    )
    assert proc.stdout is not None
    f = log_path.open("ab") if log_path else None
    cancel_task = None
    if cancel_event is not None:
        async def _watch_cancel() -> None:
            """
            Monitor cancel_event and terminate subprocess when set.

            Uses proc.terminate() (SIGTERM) first for graceful shutdown, then escalates
            to proc.kill() (SIGKILL) if process doesn't terminate within 5 seconds.

            Note: PyTorch/torchrun may ignore SIGTERM and continue rendering to completion.
            This function ensures forceful termination via SIGKILL if needed.

            IMPORTANT: We don't call proc.wait() here to avoid deadlock - the main loop
            will detect process exit when stdout closes. We just send signals with timing.
            """
            try:
                await cancel_event.wait()
                if proc.returncode is None:
                    if logger:
                        logger.info("%s: terminating due to cancel (SIGTERM)", prefix or "subprocess")
                    try:
                        # Send SIGTERM to entire process group (graceful)
                        import os
                        import signal
                        os.killpg(proc.pid, signal.SIGTERM)
                    except (ProcessLookupError, OSError):
                        return

                    # Wait 5 seconds, then escalate to SIGKILL if still running
                    await asyncio.sleep(5.0)
                    if proc.returncode is None:
                        if logger:
                            logger.warning("%s: process did not terminate after 5s, forcing kill (SIGKILL)",
                                          prefix or "subprocess")
                        try:
                            # Kill entire process group (force)
                            # Note: SIGKILL cannot be caught/blocked, but delivery can be delayed
                            # if process is in uninterruptible I/O ('D' state). However, the
                            # await proc.wait() at the end of this function guarantees we wait
                            # for actual process exit regardless of SIGKILL delivery timing.
                            os.killpg(proc.pid, signal.SIGKILL)
                        except (ProcessLookupError, OSError):
                            pass
            except asyncio.CancelledError:
                pass

        cancel_task = asyncio.create_task(_watch_cancel())

    # Write stdin_data in background task to avoid blocking stdout reads
    stdin_task = None
    if stdin_data is not None and proc.stdin is not None:
        async def _write_stdin() -> None:
            try:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Process may have exited early
            finally:
                proc.stdin.close()
        stdin_task = asyncio.create_task(_write_stdin())

    try:
        while True:
            chunk = await proc.stdout.readline()
            if not chunk:
                break
            try:
                if f:
                    if ts_prefix:
                        # Decode, prefix timestamp, write normalized line
                        try:
                            text_for_file = chunk.decode("utf-8", errors="ignore").rstrip("\r\n")
                        except Exception:
                            text_for_file = ""
                        ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                        out_line = f"[{ts}] {text_for_file}\n".encode("utf-8", errors="ignore")
                        f.write(out_line)
                    else:
                        f.write(chunk)
                    f.flush()
            except Exception:
                pass
            try:
                # Decode line and strip only newline/carriage return suffixes.
                text = chunk.decode("utf-8", errors="ignore").rstrip("\r\n")
                # Skip logging empty/whitespace-only lines (avoid spam like "prefix: ")
                if not text.strip():
                    continue
                if on_line:
                    on_line(text)
                if logger:
                    if prefix:
                        logger.info("%s: %s", prefix, text)
                    else:
                        logger.info("%s", text)
            except Exception:
                pass
    finally:
        if f:
            try:
                f.close()
            except Exception:
                pass
        if stdin_task:
            stdin_task.cancel()
        if cancel_task:
            cancel_task.cancel()
    rc = await proc.wait()
    return rc


def prefer_variant(path: Path, *, suffix: Optional[str] = None) -> Path:
    """Prefer a sibling variant `<name>{suffix}<ext>` when present with non-zero size.
    - Example: suffix="_wav" prefers `foo_wav.mp4` over `foo.mp4`.
    - When suffix is None/empty or variant missing, returns the original path.
    """
    try:
        if not suffix:
            return path
        ext = path.suffix
        base = path.stem
        candidate = path.with_name(f"{base}{suffix}{ext}")
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
        return path
    except Exception:
        return path


def find_latest_file(root: Path, pattern: str, *, since_ts: Optional[float] = None) -> Optional[Path]:
    """Recursively search under `root` for files matching glob `pattern` and
    return the most recently modified one (optionally newer than since_ts).
    Returns None when nothing found.
    """
    latest: Optional[Path] = None
    latest_mtime = -1.0
    if not root.exists():
        return None
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
            if not fnmatch.fnmatch(p.name, pattern):
                continue
            st = p.stat()
            if since_ts is not None and st.st_mtime < since_ts:
                continue
            if st.st_size <= 0:
                continue
            if st.st_mtime > latest_mtime:
                latest_mtime = st.st_mtime
                latest = p
        except Exception:
            continue
    return latest
