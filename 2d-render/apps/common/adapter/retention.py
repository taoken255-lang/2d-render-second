from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol


class _Logger(Protocol):
    def info(self, msg: str, *args, **kwargs) -> None: ...
    def warning(self, msg: str, *args, **kwargs) -> None: ...


def apply_stage_retention(*, jobs_root: Path, current: Path, keep: int, logger: _Logger) -> None:
    """
    Retain the last N stage directories under jobs_root by modification time.
    - keep <= 0: delete the current stage directory.
    - keep > 0: ensure current is kept; prune older directories beyond N.
    """
    if keep <= 0:
        shutil.rmtree(current, ignore_errors=True)
        logger.info("adapter: cleaned stage dir path=%s", str(current))
        return

    # Keep current; prune older beyond N
    try:
        entries = [p for p in jobs_root.iterdir() if p.is_dir()]
    except FileNotFoundError:
        entries = []

    # Sort by mtime desc (most recent first)
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    # Ensure current is in the list (it should be); if not, include it first
    if current not in entries and current.exists():
        entries.insert(0, current)

    to_prune = entries[keep:]
    for old in to_prune:
        try:
            shutil.rmtree(old, ignore_errors=True)
            logger.info("adapter: pruned old stage path=%s", str(old))
        except Exception as e:
            logger.warning("adapter: prune failed for %s: %s", str(old), e)

    logger.info("adapter: retained stage dir path=%s (keeping last %d)", str(current), keep)

