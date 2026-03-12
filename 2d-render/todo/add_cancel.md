# Cancel / Interrupt Handling

## Current State (verified 2025-12-17)

### What EXISTS:
- **Proto**: `Interrupt` message defined at [render_service.proto:40](protobuf/render_service.proto#L40), included in `oneof command` at [line 52](protobuf/render_service.proto#L52)
- **Streaming layer**: [streaming.py:384-387](service/streaming.py#L384-L387) handles proto `interrupt` command → clears audio queue → puts `InterruptObject`
- **IPC handler**: [streaming.py:244-246](service/streaming.py#L244-L246) receives `IPCDataType.INTERRUPT` → calls `render.interrupt()`
- **RenderService**: [render.py:67-68](service/render.py#L67-L68) delegates `interrupt()` → `self.sdk.interrupt()`
- **Online SDK**: [stream_pipeline_online.py:381-387](agnet/stream_pipeline_online.py#L381-L387) has `interrupt()` that drains queues

### Architecture Note:
**Both online/offline modes currently use `onlineSDK`** - see [render.py:29-34](service/render.py#L29-L34). This is a known bug tracked in [complete_offline.md](complete_offline.md). The offline pipeline exists but is NOT used.

**There is no true "no-stream offline" mode today.** The `offline` flag currently affects buffering/progress only; render pipeline is still `onlineSDK` and frames are still streamed via `RenderStream` RPC.

### What's MISSING (online SDK only):
1. **No `stop_event` signaling** - `interrupt()` only drains queues, doesn't set `stop_event`
2. **No sentinel propagation** - workers don't receive flush signal after drain
3. **No gRPC disconnect callback** - `context.add_callback` not used; only `context.is_active()` polling at [streaming.py:596](service/streaming.py#L596)
4. **No client acknowledgment** - no response sent to confirm interrupt processed
5. **Output queues not cleared** - interrupt only clears `audio_queue`, not `video_queue`/`local_queue`

### Current `interrupt()` implementation:
```python
# stream_pipeline_online.py:381-387
def interrupt(self):
    try:
        for q in self.queue_list:
            while not q.empty():
                q.get_nowait()
    except Exception:
        return
```
**Problem**: Only drains SDK queues. Workers continue running, may produce stale frames.

### Interrupt is best-effort:
Frames already rendered may still be emitted because:
- [streaming.py:385](service/streaming.py#L385) only clears `audio_queue`
- `video_queue` (render→streaming) not flushed
- `local_queue` (internal streaming) not flushed
- No generation-id gate to discard stale frames

## Impact
- Long queued audio/animation continues to render after interrupt
- Workers keep processing - `stop_event` not set
- Already-produced frames still sent to client
- Client disconnect may be missed if frames not flowing

---

## Implementation Plan: Online Cancel (KISS)

### Phase 1: Fix online SDK `interrupt()`

Update [stream_pipeline_online.py:381-387](agnet/stream_pipeline_online.py#L381-L387):

```python
def interrupt(self):
    """Graceful shutdown: stop workers and drain queues."""
    # Signal all workers to stop
    self.stop_event.set()

    # Drain all queues and send sentinels
    for q in self.queue_list:
        try:
            while not q.empty():
                q.get_nowait()
        except:
            pass
        # Put sentinel to unblock waiting workers
        try:
            q.put_nowait(None)
        except:
            pass
```

### Phase 2: Ensure workers handle sentinel

Workers already check `stop_event` and handle `None` - verify each worker breaks on sentinel:
```python
item = self.XXX_queue.get(timeout=1)
if item is None:
    break  # Most workers already do this via sentinel in close()
```

### Phase 3: Clear output queues on interrupt

Flush both `video_queue` and `local_queue` (or gate by generation id).

In [streaming.py](service/streaming.py) `reader_thread`, after clearing `audio_queue`:
```python
elif chunk.WhichOneof("command") == "interrupt":
    clear_queue(audio_queue)
    clear_queue(video_queue)  # ADD: flush render→streaming queue
    audio_queue.put(IPCObject(data_type=IPCDataType.INTERRUPT, ...))
```

Also need to flush `local_queue` in `RenderStream` or implement generation-id gating to discard stale frames post-interrupt.

### Phase 4: Add disconnect callback

In [streaming.py](service/streaming.py) `RenderStream`, add context callback for client disconnect:
```python
def on_disconnect():
    logger.info("Client disconnected, triggering interrupt")
    # Signal render process to stop
    clear_queue(audio_queue)
    audio_queue.put(None)  # Sentinel to stop render process

context.add_callback(on_disconnect)
```

**Why needed**: `RenderStream` blocks on `local_queue.get()` at [line 540](service/streaming.py#L540) and only checks `context.is_active()` after dequeue. If frames aren't flowing, disconnect is never detected.

---

## Future: Offline Cancel

Blocked by [complete_offline.md](complete_offline.md) - offline pipeline not currently used.

**When offline mode is restored:**
- Today there is no true "offline no-stream" mode - `RenderStream` always streams frames
- If offline is implemented as batch (no frames until done), `context.is_active()` polling is insufficient because `RenderStream` blocks on `local_queue.get()`
- Must use `context.add_callback(on_disconnect)` to detect client disconnect and stop render process
- Add similar `interrupt()` to `stream_pipeline_offline.py`
- Simpler than online: just stop immediately (no lerp to idle needed)

**gRPC backpressure warning:** If client doesn't read from stream, gRPC backpressure can block sends and stall the pipeline. True batch mode likely needs a different RPC pattern (unary "render job" + polling / file output) instead of `RenderStream`.

---

## Future: Online Lerp-to-Idle

For smoother user experience, online cancel could:
- Lerp animation state to idle pose
- Smooth transition instead of hard cut
- More complex - defer to separate task

---

## Testing Checklist

- [ ] Send interrupt during online render → verify immediate stop
- [ ] Verify no stale frames emitted after interrupt (output queues flushed)
- [ ] Check worker threads respect `stop_event`
- [ ] Test client disconnect triggers cleanup via callback
- [ ] Verify disconnect detected even when no frames flowing
