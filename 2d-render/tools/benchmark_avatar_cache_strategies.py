#!/usr/bin/env python3
"""Benchmark avatar feature-cache save strategies.

The benchmark compares:

1. list_memmap: current low-risk fix, where f_s tensors are accumulated in a
   Python list and then written to a memmapped .npy file.
2. stream_memmap: target approach, where each f_s tensor is written to the
   memmapped .npy file immediately and then released.

It intentionally measures every mode in a fresh subprocess so peak memory
values are independent.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch


STATUS_KEYS = ("VmRSS", "VmHWM", "RssAnon", "RssFile")


def _parse_shape(value: str) -> tuple[int, ...]:
    try:
        shape = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid shape: {value!r}") from exc
    if not shape or any(dim <= 0 for dim in shape):
        raise argparse.ArgumentTypeError("shape must contain positive integers")
    return shape


def _read_proc_status_kib() -> dict[str, int]:
    result: dict[str, int] = {}
    with open("/proc/self/status", "r", encoding="utf-8") as f:
        for line in f:
            name, _, rest = line.partition(":")
            if name in STATUS_KEYS:
                result[name] = int(rest.strip().split()[0])
    return result


def _kib_to_mib(value: int) -> float:
    return value / 1024.0


@dataclass
class MemoryMonitor:
    interval: float

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.peaks_kib = {key: 0 for key in STATUS_KEYS}

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, float]:
        self._stop.set()
        self._thread.join(timeout=max(self.interval * 4, 1.0))
        self._sample()
        return {f"peak_{key.lower()}_mib": _kib_to_mib(value) for key, value in self.peaks_kib.items()}

    def _sample(self) -> None:
        current = _read_proc_status_kib()
        for key, value in current.items():
            self.peaks_kib[key] = max(self.peaks_kib.get(key, 0), value)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self.interval)


def _make_tensor(shape: tuple[int, ...], idx: int) -> torch.Tensor:
    tensor = torch.empty(shape, dtype=torch.float16, device="cpu")
    tensor.fill_(idx % 2048)
    return tensor


def _to_numpy(tensor: torch.Tensor) -> np.ndarray:
    return tensor.detach().cpu().numpy().astype(np.float16, copy=False)


def _open_feature_npy(path: Path, frames: int, feature_shape: tuple[int, ...]) -> np.memmap:
    return np.lib.format.open_memmap(
        path,
        mode="w+",
        dtype=np.float16,
        shape=(frames, *feature_shape),
    )


def _run_list_memmap(args: argparse.Namespace) -> dict[str, Any]:
    feature_shape = args.feature_shape
    output_path = args.out_dir / "list_memmap.npy"
    timings: dict[str, float] = {}
    monitor = MemoryMonitor(args.sample_interval)
    monitor.start()
    total_start = time.perf_counter()

    start = time.perf_counter()
    f_s_lst = [_make_tensor(feature_shape, idx) for idx in range(args.frames)]
    timings["generate_sec"] = time.perf_counter() - start

    start = time.perf_counter()
    f_s_np = _open_feature_npy(output_path, args.frames, feature_shape)
    for idx, tensor in enumerate(f_s_lst):
        f_s_np[idx] = _to_numpy(tensor)
    f_s_np.flush()
    del f_s_np
    timings["write_sec"] = time.perf_counter() - start

    timings["total_sec"] = time.perf_counter() - total_start
    peaks = monitor.stop()
    if not args.keep_files:
        output_path.unlink(missing_ok=True)
    del f_s_lst
    gc.collect()
    return {
        "mode": "list_memmap",
        "frames": args.frames,
        "feature_shape": feature_shape,
        "output_bytes": args.output_bytes,
        **timings,
        **peaks,
    }


def _run_stream_memmap(args: argparse.Namespace) -> dict[str, Any]:
    feature_shape = args.feature_shape
    output_path = args.out_dir / "stream_memmap.npy"
    timings: dict[str, float] = {"generate_sec": 0.0, "write_sec": 0.0}
    monitor = MemoryMonitor(args.sample_interval)
    monitor.start()
    total_start = time.perf_counter()

    start = time.perf_counter()
    f_s_np = _open_feature_npy(output_path, args.frames, feature_shape)
    timings["write_sec"] += time.perf_counter() - start
    for idx in range(args.frames):
        start = time.perf_counter()
        tensor = _make_tensor(feature_shape, idx)
        timings["generate_sec"] += time.perf_counter() - start

        start = time.perf_counter()
        f_s_np[idx] = _to_numpy(tensor)
        timings["write_sec"] += time.perf_counter() - start
        del tensor
    start = time.perf_counter()
    f_s_np.flush()
    del f_s_np
    timings["write_sec"] += time.perf_counter() - start

    timings["total_sec"] = time.perf_counter() - total_start
    peaks = monitor.stop()
    if not args.keep_files:
        output_path.unlink(missing_ok=True)
    gc.collect()
    return {
        "mode": "stream_memmap",
        "frames": args.frames,
        "feature_shape": feature_shape,
        "output_bytes": args.output_bytes,
        **timings,
        **peaks,
    }


def _load_reference_shape(path: Path) -> tuple[int, tuple[int, ...], np.dtype]:
    arr = np.load(path, mmap_mode="r")
    return int(arr.shape[0]), tuple(int(dim) for dim in arr.shape[1:]), arr.dtype


def _child_main(args: argparse.Namespace) -> int:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.output_bytes = int(args.frames * np.prod(args.feature_shape) * np.dtype(np.float16).itemsize)
    if args.child_mode == "list_memmap":
        result = _run_list_memmap(args)
    elif args.child_mode == "stream_memmap":
        result = _run_stream_memmap(args)
    else:
        raise ValueError(f"unknown child mode: {args.child_mode}")
    print(json.dumps(result, sort_keys=True))
    return 0


def _format_row(row: dict[str, Any]) -> str:
    output_gib = row["output_bytes"] / (1024 ** 3)
    return (
        f"{row['mode']:<15} "
        f"{row['frames']:>6} "
        f"{output_gib:>8.2f} "
        f"{row['total_sec']:>9.2f} "
        f"{row['generate_sec']:>9.2f} "
        f"{row['write_sec']:>9.2f} "
        f"{row['peak_vmrss_mib']:>10.1f} "
        f"{row['peak_rssanon_mib']:>12.1f} "
        f"{row['peak_rssfile_mib']:>12.1f}"
    )


def _parent_main(args: argparse.Namespace) -> int:
    if args.reference_npy:
        reference_frames, reference_shape, reference_dtype = _load_reference_shape(args.reference_npy)
        if reference_dtype != np.float16:
            print(f"warning: reference dtype is {reference_dtype}, benchmark still writes float16", file=sys.stderr)
        if args.feature_shape is None:
            args.feature_shape = reference_shape
        if args.frames is None:
            args.frames = min(reference_frames, 256)

    if args.feature_shape is None:
        args.feature_shape = (1, 32, 16, 64, 64)
    if args.frames is None:
        args.frames = 256

    args.out_dir.mkdir(parents=True, exist_ok=True)
    child_base = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--frames",
        str(args.frames),
        "--feature-shape",
        ",".join(str(dim) for dim in args.feature_shape),
        "--out-dir",
        str(args.out_dir),
        "--sample-interval",
        str(args.sample_interval),
    ]
    if args.keep_files:
        child_base.append("--keep-files")

    results = []
    for mode in ("list_memmap", "stream_memmap"):
        proc = subprocess.run(
            [*child_base, "--child-mode", mode],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(proc.stdout, end="")
            print(proc.stderr, end="", file=sys.stderr)
            return proc.returncode
        try:
            results.append(json.loads(proc.stdout.strip().splitlines()[-1]))
        except (IndexError, json.JSONDecodeError) as exc:
            print(proc.stdout, end="")
            print(proc.stderr, end="", file=sys.stderr)
            raise RuntimeError(f"child {mode} did not return json") from exc

    print("mode            frames data_gib total_sec   gen_sec write_sec   peak_rss peak_anon  peak_file")
    for row in results:
        print(_format_row(row))

    by_mode = {row["mode"]: row for row in results}
    list_row = by_mode["list_memmap"]
    stream_row = by_mode["stream_memmap"]
    anon_saved = list_row["peak_rssanon_mib"] - stream_row["peak_rssanon_mib"]
    rss_saved = list_row["peak_vmrss_mib"] - stream_row["peak_vmrss_mib"]
    speed_ratio = stream_row["total_sec"] / list_row["total_sec"] if list_row["total_sec"] else float("nan")
    print()
    print(f"stream/list total time ratio: {speed_ratio:.2f}x")
    print(f"stream saved peak anon: {anon_saved:.1f} MiB")
    print(f"stream saved peak rss:  {rss_saved:.1f} MiB")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
        print(f"json: {args.json_out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=None)
    parser.add_argument("--feature-shape", type=_parse_shape, default=None)
    parser.add_argument("--reference-npy", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("/tmp/avatar-cache-bench"))
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--sample-interval", type=float, default=0.05)
    parser.add_argument("--keep-files", action="store_true")
    parser.add_argument("--child-mode", choices=("list_memmap", "stream_memmap"))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.frames is not None and args.frames <= 0:
        parser.error("--frames must be positive")
    if args.sample_interval <= 0:
        parser.error("--sample-interval must be positive")
    if args.child_mode:
        if args.frames is None:
            parser.error("--frames is required in child mode")
        if args.feature_shape is None:
            parser.error("--feature-shape is required in child mode")
        return _child_main(args)
    return _parent_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
