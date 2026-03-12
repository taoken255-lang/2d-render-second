# Add Timing Logs

Add DEBUG-level timing instrumentation for critical operations.
**Primary use case:** Compare performance across GPUs (A100 vs V100) to identify bottlenecks.

---

## Design

**Location:** `service/progress/timing.py` (same folder as `tracker.py`)

**Granularity:** Per-operation totals (not per-frame). Summary only - no per-call spam.

**Process ownership:**
- Render process owns `TimingTracker` for inference timings (SDK runs there)
- Main process uses standalone `log_timing()` for alpha/post-processing

**Log format:**
```
[TIMING] load_audio2motion: 1234.56ms
[TIMING] summary: inference_audio2motion=500ms, inference_wav2feat=100ms(25x), total=1801ms
```

**Design decision: No `enabled` param in `log_timing()`**

| Tool | Has `enabled`? | Why |
|------|---------------|-----|
| `log_timing()` | No | One-shot ops (model load, avatar setup). ~500ns overhead irrelevant vs seconds of work. loguru filters DEBUG automatically. |
| `TimingTracker` | Yes | Hot loop (100+ calls/request). `enabled=False` skips measurement entirely. |

KISS: Don't add complexity where it provides no value.

---

## Implementation

### File: `service/progress/timing.py`

```python
"""Timing utilities for DEBUG-level performance logging.

Production behavior (LOG_LEVEL != DEBUG):
- log_timing(): ~500ns overhead (loguru filters output, no I/O)
- TimingTracker: Zero overhead when enabled=False (true no-op)
"""
import time
from contextlib import contextmanager
from collections import defaultdict
from loguru import logger


@contextmanager
def log_timing(operation: str):
    """Standalone timing context manager. Logs immediately at DEBUG level.

    Production: loguru filters DEBUG -> no log output, no I/O.
    Overhead (~500ns) is irrelevant for one-shot operations like model loading.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        # Filtered by loguru when LOG_LEVEL != DEBUG - no I/O overhead
        logger.debug(f"[TIMING] {operation}: {elapsed_ms:.2f}ms")


class TimingTracker:
    """Aggregates timing measurements and provides summary.

    Production (enabled=False): measure() is a true no-op - no timing,
    no logging, zero performance impact.

    Default: accumulate silently, log only on summary (avoids per-frame spam).
    """

    def __init__(self, enabled: bool = True, log_each: bool = False):
        """
        Args:
            enabled: If False, measure() is a no-op (zero overhead in production)
            log_each: If True, log each measurement immediately (spam warning)
        """
        self.enabled = enabled
        self.log_each = log_each
        self._timings = defaultdict(list)

    @contextmanager
    def measure(self, operation: str):
        """Measure and accumulate time for an operation.

        When enabled=False: immediately yields and returns (no timing, no logging).
        """
        if not self.enabled:
            # Production: true no-op, zero overhead
            yield
            return
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self._timings[operation].append(elapsed_ms)
            if self.log_each:
                logger.debug(f"[TIMING] {operation}: {elapsed_ms:.2f}ms")

    def log_summary(self):
        """Log aggregated summary at DEBUG level."""
        if not self.enabled or not self._timings:
            return
        parts = []
        total = 0.0
        for op, times in self._timings.items():
            op_total = sum(times)
            count = len(times)
            total += op_total
            if count == 1:
                parts.append(f"{op}={op_total:.0f}ms")
            else:
                parts.append(f"{op}={op_total:.0f}ms({count}x)")
        parts.append(f"total={total:.0f}ms")
        logger.debug(f"[TIMING] summary: {', '.join(parts)}")

    def reset(self):
        """Clear all timings."""
        self._timings.clear()
```

---

## Instrumentation Points

### 1. Model Loading (`agnet/stream_pipeline_online.py` `__init__`)

One-time loads - use standalone `log_timing()`:

```python
from service.progress.timing import log_timing

with log_timing("load_avatar_registrar"):
    self.avatar_registrar = AvatarRegistrar(...)  # BlazeFace + MediaPipe + MotionExtractor + AppearanceExtractor

with log_timing("load_audio2motion"):
    self.audio2motion = Audio2Motion(...)
with log_timing("load_motion_stitch"):
    self.motion_stitch = MotionStitch(...)
with log_timing("load_warp_f3d"):
    self.warp_f3d = WarpF3D(...)
with log_timing("load_decode_f3d"):
    self.decode_f3d = DecodeF3D(...)
with log_timing("load_wav2feat"):
    self.wav2feat = Wav2Feat(...)
```

### 2. Avatar Setup (`agnet/stream_pipeline_online.py` `setup()`)

**Outer timing (total):**
```python
with log_timing("avatar_registration"):
    self.source_info = self.avatar_registrar(...)
```

**Inner breakdown** (add inside `AvatarRegistrar.__call__()` in [avatar_registrar.py](../agnet/core/atomic_components/avatar_registrar.py)):

```python
# Cache check
with log_timing("avatar_cache_load"):
    cached = self._load_from_cache(...)

# Video/image decode + resize (if not cached)
with log_timing("avatar_decode_resize"):
    frames = self._decode_and_resize(...)

# Face detection + landmarks + feature extraction (per-frame, but ~1 frame for image)
with log_timing("avatar_source2info"):
    info = self.source2info(frame, ...)  # BlazeFace + MediaPipe + MotionExtractor + AppearanceExtractor

# Cache save
with log_timing("avatar_cache_save"):
    self._save_to_cache(...)
```

This reveals if bottleneck is cache I/O, video decode, or neural nets (source2info).

### 3. Per-Request Inference (render process)

**Create tracker in render process** (not streaming.py - SDK runs in subprocess):

```python
# In start_render_process() or RenderService
from service.progress.timing import TimingTracker
from config import Config

# Production (LOG_LEVEL=INFO): enabled=False -> measure() is no-op, zero overhead
# Debug (LOG_LEVEL=DEBUG): enabled=True -> collects timings, logs summary
timing = TimingTracker(
    enabled=(Config.LOG_LEVEL == "DEBUG"),
    log_each=False  # summary only, avoid per-call spam
)
```

**Audio preprocessing (offline mode):**

```python
# Audio decode + resample (CPU work, can be significant)
with timing.measure("audio_decode_resample"):
    audio, sr = librosa.load(wav_buffer, sr=16000)
```

**Wrap all 5 model inferences:**

```python
# Audio features (Wav2Feat / HuBERT) - converts audio to features
with timing.measure("inference_wav2feat"):
    aud_feat = self.sdk.wav2feat.wav2feat(...)

# Motion generation
with timing.measure("inference_audio2motion"):
    output = self.sdk.audio2motion(...)

with timing.measure("inference_motion_stitch"):
    x_s, x_d = self.sdk.motion_stitch(...)

# Frame synthesis
with timing.measure("inference_warp_f3d"):
    f_3d = self.sdk.warp_f3d(...)

with timing.measure("inference_decode_f3d"):
    frame = self.sdk.decode_f3d(...)
```

**Log summary before render.sdk.close():**

```python
timing.log_summary()
render.sdk.close()
```

### 4. Alpha Service (`service/offline_alpha.py`) - Main Process

BiRefNet runs in main process, use standalone `log_timing()`:

```python
from service.progress.timing import log_timing

with log_timing("inference_birefnet"):
    result = self.model(input)
```

---

## Files to Modify

| File | Changes |
|------|---------|
| `service/progress/timing.py` | **NEW** - TimingTracker + log_timing |
| `agnet/stream_pipeline_online.py` | Add timing to `__init__`, `setup()` |
| `agnet/core/atomic_components/avatar_registrar.py` | Add timing breakdown (cache, decode, source2info) |
| `service/render.py` or `start_render_process` | Create tracker, wrap inference + audio decode, log summary |
| `service/offline_alpha.py` | Add timing to BiRefNet inference |

**Not modified:** `service/streaming.py` - tracker lives in render process, not here.

---

## Testing

1. Set `LOG_LEVEL=DEBUG` in env
2. Run render request
3. Verify timing logs appear for:

   **Model Loading (one-time):**
   - `load_avatar_registrar`
   - `load_audio2motion`, `load_motion_stitch`, `load_warp_f3d`, `load_decode_f3d`, `load_wav2feat`

   **Avatar Setup (per-request):**
   - `avatar_registration` (total)
   - `avatar_cache_load`, `avatar_decode_resize`, `avatar_source2info`, `avatar_cache_save`

   **Inference Summary (aggregated):**
   - `audio_decode_resample` (offline mode)
   - `inference_wav2feat`, `inference_audio2motion`, `inference_motion_stitch`
   - `inference_warp_f3d`, `inference_decode_f3d`

   **Alpha:**
   - `inference_birefnet`

4. Verify: Summary shows `(Nx)` suffix for repeated ops
5. Confirm no timing logs when `LOG_LEVEL=INFO`
6. Compare A100 vs V100 output to identify bottlenecks
