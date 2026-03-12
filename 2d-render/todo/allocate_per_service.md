# Per-Service Model Allocation

Optimize model loading strategy to reduce cold-start latency.

---

## Problem

Each request spawns new process -> loads ALL models -> renders -> exits.

**Key metric:** `time_ready_to_render - time_request_received`

| State | Latency |
|-------|---------|
| Current | Several seconds (model loading each request) |
| Target | <1s (models already loaded) |

---

## Architecture

**Rule:** Only one layer should schedule GPU work.

```
[Async 2D Render Workers] -> gRPC -> [2d-render service] -> GPU
        ^                                    ^
   owns concurrency              just load once + semaphore
```

- Upstream workers (Rabbit) own scheduling/concurrency
- 2d-render is GPU model host, not scheduler
- Run 1 instance per GPU (pin with `CUDA_VISIBLE_DEVICES`)
- Return `RESOURCE_EXHAUSTED` when busy, upstream retries

---

## Two Types of "Workers"

| Type | What | Limit by | Keep/Change |
|------|------|----------|-------------|
| gRPC threads | `ThreadPoolExecutor(max_workers=10)` | CPU/network | Keep 10 (battle-tested) |
| GPU slots | Render execution | VRAM | Add semaphore (1-2) |

**Why separate:** gRPC threads handle network I/O (streaming RPCs can block). GPU slots limit actual VRAM usage. 10 gRPC threads ≠ 10 GPU renders.

---

## Solution: Long-Lived Engine + GPU Gate

**Load all models at startup, reuse for all requests:**

| Model | When Loaded | Lifetime |
|-------|-------------|----------|
| BiRefNet (alpha) | Server startup | Server process |
| StreamSDK (render) | Server startup | Server process |

**Instead of per-request subprocess:** Keep models in memory, gate GPU concurrency with semaphore.

### How It Works

```
STARTUP:
1. server.py starts
2. OfflineAlphaService() loads BiRefNet (~2GB VRAM)
3. StreamingService._ensure_sdk_loaded() loads StreamSDK (~20GB VRAM)
4. Mark /ready = true
5. Start accepting gRPC requests

PER REQUEST:
1. RenderStream() called
2. GPU_SLOTS.acquire() - if busy, return RESOURCE_EXHAUSTED
3. sdk.setup(avatar) - configure for this avatar (no model loading)
4. for chunk in request: sdk.run_chunk() -> yield frames
5. sdk.close() - cleanup threads/queues (models stay loaded)
6. GPU_SLOTS.release()
```

### Code Example

```python
# Reject-fast GPU gate (Rabbit stays the only queue)
import threading
GPU_SLOTS = threading.Semaphore(int(os.getenv("MAX_CONCURRENT", "1")))

def RenderStream(self, request_iterator, context):
    if not GPU_SLOTS.acquire(blocking=False):
        context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
        context.set_details("busy")
        return
    try:
        # render job using pre-loaded models
        ...
    finally:
        GPU_SLOTS.release()
```

**Expose:**
- `/health` - service alive
- `/ready` - models loaded
- `/capacity` (optional) - current slots available

---

## Risk: StreamSDK Reusability

**Analysis:** StreamSDK is reusable after `close()` if previous run completed cleanly. If a worker exception sets `stop_event`, the instance is broken.

| Component | Loaded in | Reusable? |
|-----------|-----------|-----------|
| ML models (avatar_registrar, audio2motion, etc.) | `__init__` | Yes - heavy, load once |
| Threads, queues, state | `setup()` | Yes - recreated each call |
| `stop_event` | `__init__` | No - never cleared after exception |

**Fix required in `StreamSDK.setup()`:**
```python
# Reset state for reuse
self.stop_event.clear()
self.worker_exception = None
```

- [ ] Add reset logic to `StreamSDK.setup()` before implementing long-lived process

---

## Implementation

**Keep GitLab CI as-is** - only change env files and service code.

### Phase 0: Config (lowest risk)

Update env files (`ENV_V100`, `ENV_A100`, `ENV_4090`, etc.):

```env
MAX_CONCURRENT=1
CUDA_VISIBLE_DEVICES=0  # each container on same host must have different value (0,1,2...)
```

- [ ] Add `MAX_CONCURRENT=1` to all env files
- [ ] Add `CUDA_VISIBLE_DEVICES` pinning (different value per container on same host)
- [ ] Verify: no two containers share same GPU

### Phase 1: Long-Lived Engine + GPU Gate (in-process)

**Important:** `MAX_CONCURRENT=1` requires single SDK. For `MAX_CONCURRENT>1`, need SDK pool (each loads models - VRAM doubles).

#### 1.1 Load StreamSDK once at startup

**File:** [service/streaming.py](../service/streaming.py)

```python
# Before (per-request):
def RenderStream(self, request_iterator, context):
    render_process = Process(target=start_render_process, args=(...))
    render_process.start()

# After (long-lived, MAX_CONCURRENT=1):
class StreamingService:
    def __init__(self, alpha_service):
        self.alpha_service = alpha_service
        self.sdk = None
        self._sdk_lock = threading.Lock()  # prevent race during init

    def _ensure_sdk_loaded(self):
        with self._sdk_lock:
            if self.sdk is None:
                self.sdk = RenderService(...)  # loads StreamSDK once
```

#### 1.2 Add reset logic to StreamSDK

**File:** [agnet/stream_pipeline_online.py](../agnet/stream_pipeline_online.py) `StreamSDK.setup()`

```python
def setup(self, ...):
    # Add at start of setup()
    self.stop_event.clear()
    self.worker_exception = None
    # ... rest of existing setup code
```

#### 1.3 Add GPU gate semaphore

**File:** [service/streaming.py](../service/streaming.py)

```python
import threading
GPU_SLOTS = threading.Semaphore(int(os.getenv("MAX_CONCURRENT", "1")))

def RenderStream(self, request_iterator, context):
    if not GPU_SLOTS.acquire(blocking=False):
        context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
        context.set_details("busy")
        return
    try:
        self.sdk.setup(avatar, ...)
        try:
            for chunk in request_iterator:
                ...
                yield RenderResponse(...)
        finally:
            self.sdk.close()  # always cleanup per-request threads/queues
    finally:
        GPU_SLOTS.release()
```

#### 1.4 Mark `/ready` after models loaded

**File:** [server.py](../server.py)

```python
# Add health check endpoint or flag
ready = threading.Event()

def grpc_service():
    alpha_service = OfflineAlphaService()  # loads BiRefNet
    streaming_service = StreamingService(alpha_service)
    streaming_service._ensure_sdk_loaded()  # loads StreamSDK
    ready.set()  # now ready to serve
    # ... start server
```

### Phase 2: Measure & Tune

- [ ] Measure VRAM: `nvidia-smi` during render
- [ ] If headroom exists on 80GB GPUs, try `MAX_CONCURRENT=2`

---

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `MAX_CONCURRENT` | 1 | Concurrent renders per GPU |

### MAX_CONCURRENT=1 (default, single SDK)

Single shared `self.sdk` + semaphore. Simple, safe.

### MAX_CONCURRENT>1 (SDK pool, VRAM doubles)

**Only if VRAM allows.** Replace single SDK with pool:

```python
import queue
SDK_POOL = queue.SimpleQueue()
for _ in range(MAX_CONCURRENT):
    SDK_POOL.put(RenderService(...))  # each loads models

def RenderStream(self, request_iterator, context):
    try:
        sdk = SDK_POOL.get_nowait()
    except queue.Empty:
        context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
        return
    try:
        sdk.setup(...)
        ...
        sdk.close()
    finally:
        SDK_POOL.put(sdk)
```

**When to try MAX_CONCURRENT=2:**
```bash
nvidia-smi --query-gpu=memory.total,memory.used,utilization.gpu --format=csv -l 1
```
- Peak VRAM leaves <40% headroom (need room for 2nd SDK) -> try 2
- GPU util low + VRAM headroom -> try 2
- Otherwise stay at 1

---

## References

- Current architecture: [docs/system/memory_allocation.md](../docs/system/memory_allocation.md)
- [PyTorch multiprocessing best practices](https://docs.pytorch.org/docs/stable/notes/multiprocessing.html) - spawn required for CUDA
- [NVIDIA Triton optimization](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/optimization.html) - dynamic batching, instance count
