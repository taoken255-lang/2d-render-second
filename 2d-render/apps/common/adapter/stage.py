"""
Per-job staging directory structure for adapters.

Every job gets an isolated directory tree:
  /work/jobs/{job_id}/
    ├── inputs/   (audio.wav, photo.<ext>)
    ├── outputs/  (video.mp4)
    └── logs/     (run.txt, upload.txt)

This isolation enables:
- Safe concurrent job execution
- Post-mortem debugging (retain last N job dirs)
- Clean separation of inputs/outputs/logs
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Stage:
    """
    Per-job staging directory structure.

    Attributes:
        root: Base job directory (/work/jobs/{job_id})
        inputs: Input staging dir (fetched audio/photo)
        outputs: Output staging dir (rendered video before upload)
        logs: Log dir (engine stdout/stderr, upload logs)
    """
    root: Path
    inputs: Path
    outputs: Path
    logs: Path

    @classmethod
    def create(cls, base: Path, job_id: str) -> "Stage":
        """
        Create per-job staging directories.

        Args:
            base: Base work directory (e.g., /work)
            job_id: Unique job identifier

        Returns:
            Stage instance with created directories

        Example:
            stage = Stage.create(Path("/work"), "abc123")
            # Creates:
            #   /work/jobs/abc123/inputs/
            #   /work/jobs/abc123/outputs/
            #   /work/jobs/abc123/logs/
        """
        root = base / "jobs" / job_id
        inputs = root / "inputs"
        outputs = root / "outputs"
        logs = root / "logs"
        for p in (inputs, outputs, logs):
            p.mkdir(parents=True, exist_ok=True)
        return cls(root=root, inputs=inputs, outputs=outputs, logs=logs)
