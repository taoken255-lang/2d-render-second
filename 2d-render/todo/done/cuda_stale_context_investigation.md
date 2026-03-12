# CUDA Stale Context Investigation

> **⚠️ MISLEADING INVESTIGATION - SEE ACTUAL FIX BELOW**
>
> This document investigated application-level CUDA context issues (memory cleanup, TensorRT state).
> **Actual root cause:** Container loses GPU device access at OS level due to Docker cgroup driver issue.
> The error `"Error during initialization of network: "` (empty message) occurs because `torch.cuda.is_available()` returns `False` - device gone, not context stale.
>
> **Actual fix:** [docs/infra/gpu_access_loss.md](../docs/infra/gpu_access_loss.md)

---

## Problem Description

After extended idle periods (e.g., 2 days), the Ditto gRPC service fails on the first request with "CUDA is not available" error. The service auto-kills and restarts, then the second request succeeds.

**Observed behavior:**
```
First request  → "CUDA is not available" → Service crashes → Auto-restart
Second request → Success (fresh CUDA context)
```

**Key question:** Why does Finik (repos/infinite) not suffer from this issue while Ditto does?

---

## Comparative Analysis: Finik vs Ditto

### Architecture Differences

| Aspect | Finik | Ditto |
|--------|-------|-------|
| **Service Type** | FastAPI HTTP Adapter | gRPC Streaming Service |
| **GPU Concurrency** | Semaphore(1) - serial | ThreadPoolExecutor(10) - concurrent |
| **Job Isolation** | Clean boundaries per job | Overlapping streams possible |
| **Container Init** | `tini` (proper PID 1) | Direct python (PID 1 issues) |

### GPU Resource Management

| Aspect | Finik | Ditto |
|--------|-------|-------|
| **GPU Cleanup** | `torch.cuda.empty_cache()` + `gc.collect()` after each job | **None** |
| **Inter-job Delay** | 1 second grace period for OS cleanup | Immediate reuse |
| **CUDA Allocator** | Configurable (`expandable_segments:True`) | Default |
| **Memory Config** | `max_split_size_mb:64, garbage_collection_threshold:0.6` | None |

### Health & Recovery

| Aspect | Finik | Ditto |
|--------|-------|-------|
| **Health Check** | HTTP `/healthz` every 30s (app-level) | gRPC keepalive (TCP level only) |
| **Watchdog** | Detects stalls, triggers restart after 27min | **None** |
| **Stall Detection** | Cancel-stall (60s) + General-stall (27min) | **None** |

---

## Finik's GPU Cleanup Code

Location: `repos/infinitetalk/finik_comfyui.py` (lines 708-729)

```python
def _cleanup_on_cancel(self) -> None:
    """Release resources on cancel: tensors, file handles, CUDA cache."""
    try:
        import gc
        gc.collect()  # Force garbage collection

        if torch.cuda.is_available():
            torch.cuda.empty_cache()  # Clear CUDA memory cache
            torch.cuda.synchronize()  # Ensure all CUDA ops complete

        logger.info("[finik] Cleanup on cancel: gc + CUDA cache cleared")
    except Exception as e:
        logger.warning("[finik] Cleanup error: %s", e)
```

This is called after **every job** (success or cancel) via the adapter layer.

---

## Root Cause Hypothesis

The issue is likely **NOT the NVIDIA driver** but rather:

1. **Accumulated CUDA context garbage** - Without periodic cleanup, GPU memory fragments and CUDA contexts accumulate stale references over days

2. **No GPU cache clearing** - Ditto never calls `torch.cuda.empty_cache()`, allowing memory to fragment indefinitely

3. **TensorRT engine state drift** - Long-running TensorRT contexts may have internal state that becomes inconsistent

4. **No watchdog recovery** - Unlike Finik, Ditto has no mechanism to detect and recover from stale GPU state

5. **Concurrent GPU access** - 10 worker threads may cause CUDA context interference (Finik uses Semaphore(1))

### Why First Request Fails, Second Succeeds

1. First request attempts to use stale CUDA context
2. PyTorch/TensorRT detects invalid state, raises error
3. gRPC service crashes, container restarts
4. Fresh service = fresh CUDA context
5. Second request works

---

## Potential Fixes

### Option 1: Minimal Fix (Low Risk)

Add GPU cleanup after each render stream completes.

**Location:** `service/streaming.py` - end of `RenderStream()` method

```python
# After stream completes (success or error)
try:
    import gc
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
    logger.info("CUDA cache cleared after stream")
except Exception as e:
    logger.warning(f"CUDA cleanup failed: {e}")
```

### Option 2: Periodic Health Check (Medium)

Add background thread that validates CUDA and clears cache periodically.

```python
import threading
import time

def cuda_keepalive(interval_sec=300):
    """Background thread: validate CUDA every N seconds."""
    while True:
        time.sleep(interval_sec)
        try:
            if torch.cuda.is_available():
                # Small operation to keep context alive
                _ = torch.zeros(1, device='cuda')
                torch.cuda.empty_cache()
                logger.debug("CUDA keepalive: OK")
            else:
                logger.error("CUDA keepalive: CUDA not available!")
        except Exception as e:
            logger.error(f"CUDA keepalive failed: {e}")

# Start in server.py
threading.Thread(target=cuda_keepalive, daemon=True).start()
```

### Option 3: Full Solution (Like Finik)

1. Add GPU cleanup after each stream
2. Add periodic CUDA keepalive
3. Add watchdog with stall detection
4. Add gRPC health check service that validates CUDA
5. Configure proper CUDA allocator settings
6. Use `tini` as container init

---

## Investigation Steps for Future Issues

### 1. Check CUDA State Inside Container

```bash
# Enter running container
docker exec -it <container> bash

# Check NVIDIA driver
nvidia-smi

# Check PyTorch CUDA
python3 -c "import torch; print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0))"

# Check CUDA memory
python3 -c "import torch; print(torch.cuda.memory_summary())"
```

### 2. Check Host GPU State

```bash
# Persistence mode (should be ON for long-running services)
nvidia-smi -q | grep "Persistence Mode"

# Enable if needed
sudo nvidia-smi -pm 1

# Check power state
nvidia-smi -q | grep -E "(Power|Performance)"

# Check for other GPU processes
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
```

### 3. Add Diagnostic Logging

Add to `server.py` startup:

```python
logger.info(f"CUDA available: {torch.cuda.is_available()}")
logger.info(f"CUDA device: {torch.cuda.get_device_name(0)}")
logger.info(f"CUDA memory: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")
```

Add periodic logging (every 5 min):

```python
logger.info(f"CUDA memory allocated: {torch.cuda.memory_allocated() / 1e6:.1f} MB")
logger.info(f"CUDA memory cached: {torch.cuda.memory_reserved() / 1e6:.1f} MB")
```

### 4. Monitor for Memory Leaks

```bash
# Watch GPU memory over time
watch -n 60 nvidia-smi --query-gpu=memory.used,memory.free --format=csv
```

---

## Recommended Logging Additions

### In `service/streaming.py`

```python
# At stream start
logger.info(f"[RenderStream] Start - CUDA mem: {torch.cuda.memory_allocated()/1e6:.1f}MB allocated, {torch.cuda.memory_reserved()/1e6:.1f}MB reserved")

# At stream end
logger.info(f"[RenderStream] End - CUDA mem: {torch.cuda.memory_allocated()/1e6:.1f}MB allocated, {torch.cuda.memory_reserved()/1e6:.1f}MB reserved")
```

### In `server.py` (periodic)

```python
def log_gpu_status():
    """Log GPU status every 5 minutes."""
    while True:
        time.sleep(300)
        try:
            allocated = torch.cuda.memory_allocated() / 1e6
            reserved = torch.cuda.memory_reserved() / 1e6
            logger.info(f"[GPU Status] allocated={allocated:.1f}MB reserved={reserved:.1f}MB")
        except Exception as e:
            logger.error(f"[GPU Status] Failed: {e}")
```

---

## Docker Configuration Recommendations

### Add to Dockerfile

```dockerfile
# Proper init system
RUN apt-get update && apt-get install -y tini
ENTRYPOINT ["/usr/bin/tini", "--"]

# CUDA memory settings
ENV PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
```

### Add Health Check

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import torch; assert torch.cuda.is_available()"
```

---

## Summary

**Root cause:** Ditto lacks GPU resource cleanup that Finik has, causing CUDA context to become stale after extended idle periods.

**Minimum fix:** Add `torch.cuda.empty_cache()` + `gc.collect()` after each render stream.

**Better fix:** Add periodic CUDA keepalive + watchdog + health checks.

**Investigation:** Use logging additions above to track GPU memory over time and detect patterns.
