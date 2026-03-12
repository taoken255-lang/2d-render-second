# Progress Tracking Support

## Current State

**Limitation:** Client can track absolute frame count but NOT percentage progress.

### What Works
- `VideoChunk.frame_idx` ([protobuf/render_service.proto:84](../protobuf/render_service.proto#L84)) - incrementing counter
- `StreamingService.RenderStream` ([service/streaming.py:580-587](../service/streaming.py#L580-L587)) - increments `frame_idx` per frame
- Client receives frames sequentially with index

### What's Missing
- **No server-sent `progress_percent`** in the stream (clients only see sequential frames)
- Optional fields (for richer UIs) are absent:
  - `frames_completed` + `frames_total` for displays like "450/1000 (45%)"
  - timestamps or totals for ETA ("30s remaining")
- Total frames remain unpredictable (animations/emotions/audio), so server-calculated percentage is the minimal, reliable signal

### Impact
- UIs cannot show progress bars with percentage (e.g., "45% complete")
- Clients only know "N frames received" not "N of M frames"
- Poor UX for long renders - users don't know how much time remains

---

## Best Practice: Server Calculates, Client Displays

### Core Principle

**Heavy computation belongs on the server.**

| Responsibility | Owner | Why |
|----------------|-------|-----|
| Calculate total frames | Server | Complex logic: audio + animations + emotions |
| Calculate progress % | Server | Server knows rendering plan and current state |
| Display progress | Client | Simple presentation (just show the number) |

**Why server-side calculation:**
- Server owns rendering pipeline logic
- Client shouldn't parse audio files or know animation durations
- Total can change dynamically (new requests mid-stream)
- Simpler clients = easier integration

### Industry Examples

**Google Cloud Speech API** calculates `progress_percent` server-side:
```protobuf
message LongRunningRecognizeMetadata {
  // Server calculates this
  int32 progress_percent = 1;
  google.protobuf.Timestamp start_time = 2;
  google.protobuf.Timestamp last_update_time = 3;
}
```
[Source](https://github.com/googleapis/googleapis/blob/master/google/cloud/speech/v1/cloud_speech.proto)

**Google Service Management API** does the same:
```protobuf
message OperationMetadata {
  // Server calculates this
  int32 progress_percentage = 3;
  google.protobuf.Timestamp start_time = 4;
}
```
[Source](https://github.com/googleapis/googleapis/blob/master/google/api/servicemanagement/v1/resources.proto)

**Pattern:** Server sends `int32` percentage (0-100), client displays it. Simple.

---

## Recommended Solution: `RenderMetadata` Message

**Approach:** Add metadata message to streaming response (backward compatible)

- **Always sent** (both modes): `frames_completed`, `start_time`, `last_update_time`
- **Offline mode only**: `progress_percent`, `frames_total` (when total is known)
- **Online mode only**: `realtime_metric` (latency tracking)

### Protobuf Schema

```protobuf
message Status {
    // Core metrics (always present in both modes)
    // Scalars need `optional` for HasField(), messages have implicit presence
    optional uint64 frames_completed = 1;        // Scalar - needs optional
    google.protobuf.Timestamp start_time = 2;    // Message - implicit presence
    google.protobuf.Timestamp last_update_time = 3;

    // Mode-specific (optional scalars)
    optional int32 progress_percent = 10;      // Offline only
    optional uint64 frames_total = 11;          // Offline only
    optional float realtime_metric = 12;        // Online only

    // Future extensions (backward compatible):
    // - ResourceUsage resources = 13;
    // - QualityMetrics quality = 14;
}

message RenderMetadata {
    Status status = 1;
}

message RenderResponse {
    oneof chunk {
        VideoChunk video = 1;
        StartAnimation start_animation = 2;
        EndAnimation end_animation = 3;
        AvatarSet avatar_set = 4;
        EmotionSet emotion_set = 5;
        RequestError request_error = 6;
        // Backward compatible addition; old clients ignore field 7.
        RenderMetadata metadata = 7;
    }
}
```

### Why This Design

✅ **Nested Encapsulation**
- All status data grouped in single `Status` message
- Simpler client access: `response.metadata.status.frames_completed`
- Better naming: "Status" vs "ProgressStatus" (no redundancy)
- Future-proof: extend `Status` without restructuring `RenderMetadata`

✅ **Backward Compatible**
- Old clients ignore unknown field #7 and any new fields inside `RenderMetadata`
- New clients can display percentage, totals, and ETAs if present
- No renumbering or breaking changes to existing fields

✅ **Follows Industry Standards**
- Matches Google's LRO pattern
- Server calculates complex progress logic
- Client gets simple percentage to display

✅ **Extensible**
- `RenderMetadata` can grow with optional fields (`optional` preserves presence)
- Can add resource usage, quality metrics, debug info without breaking clients
- No need for JSON escape hatches or extra `oneof`s

✅ **Minimal Bandwidth**
- Send every N frames (e.g., every 25 frames = 1 second at 25fps)
- ~20 bytes per update vs. repeated in every frame

✅ **Progress as Event, Not Metadata**
- **Inside `oneof`**: Progress is a first-class event (like `StartAnimation`, `EndAnimation`)
- **Stream pattern**: `video → video → ... → metadata → video → video → ... → metadata`
- **Client mental model**: Single switch handles all event types uniformly
- **Why NOT outside `oneof`?**
  - Would require checking two fields per message (metadata + chunk type)
  - Temptation to send with every frame (wasteful)
  - Unclear when metadata is present vs absent
- **Key insight**: Progress is sent **periodically** (every 1s), not attached to every frame

### Compatibility Notes

- Field numbers stay fixed; do not renumber existing tags.
- `RenderResponse.oneof chunk` gains field 7; old clients skip unknown field 7.
- `RenderMetadata` grows with `optional` fields; presence can be detected via `HasField`.
- No JSON wrapper or extra `oneof` needed; typed proto remains forward-compatible.

### Server-Side Design

**Approach:** Add `ProgressTracker` class in new `service/progress/` module following OCP principle

**Folder Structure:**
```
service/
├── progress/
│   ├── __init__.py      # Export ProgressTracker
│   └── tracker.py       # ProgressTracker class (~50 lines)
├── streaming.py         # Integration point (4 lines added)
├── render.py
└── alpha.py
```

**Design pattern:**
```
StreamingService (owner/caller)
    ↓ creates
ProgressTracker (passive object)
    ↓ called by service after each frame
on_frame()
    ↓ returns metadata every N frames
RenderResponse(metadata=...)
    ↓ yielded to client
Client receives progress event
```

**Why this design:**
- Clean separation: tracker logic isolated in class
- Minimal changes: ~50 lines for class + 1 line initialization + 3 lines tracking
- Easy to test and extend later
- No new files needed
- **Tracker is passive:** StreamingService owns and calls it (no callbacks/observers)
- **Explicit mode flag:** Uses `is_online` instead of inferring from `total_frames > 0` (clearer, handles edge cases)
- **No separate start():** Construction = initialization (tracker never reused)

### Client Usage

```python
# Client adapts display based on available fields (offline vs online mode)
for response in client.RenderStream(requests):
    if response.HasField("metadata"):
        status = response.metadata.status

        # Offline mode - has percentage and total
        if status.HasField("progress_percent"):
            print(f"Progress: {status.progress_percent}% ({status.frames_completed}/{status.frames_total})")

            # Calculate ETA if timestamps available
            if status.HasField("start_time") and status.progress_percent > 0:
                elapsed = (status.last_update_time.seconds - status.start_time.seconds)
                eta = (elapsed / status.progress_percent) * (100 - status.progress_percent)
                print(f"ETA: {eta:.0f}s")

        # Online mode - has latency metric
        elif status.HasField("realtime_metric"):
            state = "✅ Ahead" if status.realtime_metric < 0 else "⚠️ Lagging"
            print(f"{state} by {abs(status.realtime_metric):.2f}s | Frames: {status.frames_completed}")

        # Fallback - just show frames
        else:
            print(f"Frames rendered: {status.frames_completed}")

    elif response.HasField("video"):
        save_frame(response.video)
```

---

## Implementation Details

### 1. Update Protobuf Definition ✅

**Files created/modified:** (see [protobuf/README.md](../protobuf/README.md))
- [protobuf/status.proto](../protobuf/status.proto) - `Status` message with all fields
- [protobuf/meta.proto](../protobuf/meta.proto) - `RenderMetadata` wrapper importing status.proto
- [protobuf/render_service.proto](../protobuf/render_service.proto) - imports meta.proto, adds `metadata = 7` to oneof

**Generated Python bindings:**
- `proto/status_pb2.py` - Status class
- `proto/meta_pb2.py` - RenderMetadata class
- `proto/render_service_pb2.py` - Updated with metadata in oneof

**Command:**
```bash
uv run python -m grpc_tools.protoc -I protobuf/ --python_out=proto/ --grpc_python_out=proto/ \
    protobuf/render_service.proto protobuf/meta.proto protobuf/status.proto
```

### 2. Create Progress Module

**Files to create:**
- [service/progress/__init__.py](../service/progress/__init__.py) - Export ProgressTracker
- [service/progress/tracker.py](../service/progress/tracker.py) - ProgressTracker class (~50 lines)

**Class design** (`tracker.py`):

- `__init__(emit_interval=25, is_online=False, total_frames=0)` - create and initialize tracker
  - `emit_interval` = number of FRAMES between progress updates (not seconds)
  - `is_online` = **explicit mode flag** (True=online/streaming, False=offline/batch)
  - `total_frames` = estimated total frames (only used if `is_online=False`)
  - Immediately initializes: sets `start_time`, resets `frames_completed=0`
  - Internally ignores `total_frames` if online mode
  - **Ready to use immediately after construction**
  - Example: 25 frames = 1 second at 25fps

- `track_frame_progress(realtime_metric=None)` - **called after EVERY frame**
  - Increments internal counter: `self.frames_completed += 1`
  - Checks: `if self.frames_completed % emit_interval == 0`
  - Returns `RenderMetadata` when ready (every 25th frame)
  - Returns `None` otherwise
  - Accepts optional `realtime_metric` for online mode latency

  **Example flow:**
  ```
  Frame 1  → track_frame_progress(metric) → None (don't emit)
  Frame 2  → track_frame_progress(metric) → None
  ...
  Frame 25 → track_frame_progress(metric) → RenderMetadata (emit!) ← 1 second
  Frame 26 → track_frame_progress(metric) → None
  ...
  Frame 50 → track_frame_progress(metric) → RenderMetadata (emit!) ← 2 seconds
  ```

- `_create_metadata(realtime_metric)` - internal method
  - Creates `RenderMetadata` containing `Status` with mode-appropriate fields
  - **Always includes** in `Status`: `frames_completed`, `start_time`, `last_update_time`
  - **Offline mode** (`is_online=False`): adds `progress_percent`, `frames_total` to Status
  - **Online mode** (`is_online=True`): adds `realtime_metric` to Status
  - Returns: `RenderMetadata(status=Status(...))`

- `_calculate_progress()` - internal method
  - Computes percentage (0-100) from `frames_completed / total_frames`
  - Only called in offline mode when `not self.is_online` and `total_frames > 0`

### 3. Integrate in RenderStream

**File:** [service/streaming.py](../service/streaming.py) at line ~471

**Owner:** `StreamingService.RenderStream()` method - the main gRPC streaming handler

**3 integration points** (4 lines total):

1. **Import tracker** (at top of file)
   ```python
   from service.progress import ProgressTracker
   ```

2. **Create tracker** (at stream start, near line ~473 after `frame_idx = 0`)
   ```python
   # Create and initialize - ready to use immediately
   # TODO: Calculate total_frames from audio/animations for offline mode
   tracker = ProgressTracker(emit_interval=25, is_online=is_online.is_set(), total_frames=0)
   ```
   - **One line:** Construction + initialization combined
   - **Benefit:** Simpler, no separate start() needed

3. **Track progress after EVERY frame** (in main loop, after line ~587 `frame_idx += 1`)
   ```python
   # Reuse existing realtime_metric (line ~583)
   realtime_metric = (time.perf_counter() - first_frame_time) - frame_idx * every_frame_time

   # Track and emit progress when ready
   metadata = tracker.track_frame_progress(realtime_metric=realtime_metric)
   if metadata:
       yield RenderResponse(metadata=metadata)
   ```
   - **Note:** Tracker maintains `frames_completed` internally (separate from `frame_idx`)
   - **Why:** Separation of concerns, avoid tight coupling

### 4. Test Client Update

**File:** [infra/local/test/test_client.py](../infra/local/test/test_client.py)

**Update:** `test_render_stream` (lines 102-198) to handle `metadata` field

---

## Client-Side Workaround (Until Server Support Added)

### Audio-Based Estimation

Until server-side `RenderMetadata` is implemented, clients can **estimate** progress:

```python
def estimate_progress(audio_path: str, current_frame_idx: int) -> float:
    """
    Calculate estimated progress percentage.

    Args:
        audio_path: Path to input audio file
        current_frame_idx: Current frame from VideoChunk.frame_idx

    Returns:
        Estimated progress (0.0 to 100.0)
    """
    # Load audio metadata
    with wave.open(audio_path, 'rb') as wav:
        sample_rate = wav.getframerate()
        n_frames = wav.getnframes()
        duration_sec = n_frames / sample_rate

    # Estimate total video frames (25 fps)
    estimated_total_frames = duration_sec * 25

    # Calculate progress
    progress = (current_frame_idx / estimated_total_frames) * 100

    # Cap at 99.9% until stream ends
    return min(progress, 99.9)
```

### Usage in Test Client

Add to `test_render_stream` ([infra/local/test/test_client.py:102-198](../infra/local/test/test_client.py#L102-L198)):

```python
# Before loop
audio_duration = get_audio_duration(audio_path)
estimated_total = int(audio_duration * 25)

# In response loop
for response in responses:
    if response.HasField('video'):
        progress = (response.video.frame_idx / estimated_total) * 100
        print(f"Progress: {progress:.1f}% ({response.video.frame_idx}/{estimated_total})")
```

### Limitations of Workaround

- ⚠️ Inaccurate if animations extend beyond audio
- ⚠️ Doesn't account for emotion transitions
- ⚠️ May show >100% for long animations
- ⚠️ Client must have access to audio file
- ⚠️ Client must understand audio formats

**Workaround is acceptable for testing but NOT production-grade UX.**

---

## Implementation Checklist

### Phase 1: Protobuf Changes ✅
- [x] Create modular proto files (see [protobuf/README.md](../protobuf/README.md))
  - [x] [protobuf/status.proto](../protobuf/status.proto) - `Status` message
  - [x] [protobuf/meta.proto](../protobuf/meta.proto) - `RenderMetadata` wrapper
  - [x] Update [protobuf/render_service.proto](../protobuf/render_service.proto) - import + oneof field
- [x] `Status` message fields:
  - [x] `optional uint64 frames_completed = 1;` (scalar needs optional)
  - [x] `google.protobuf.Timestamp start_time = 2;` (message has implicit presence)
  - [x] `google.protobuf.Timestamp last_update_time = 3;`
  - [x] `optional int32 progress_percent = 10;`
  - [x] `optional uint64 frames_total = 11;`
  - [x] `optional float realtime_metric = 12;`
- [x] `RenderMetadata metadata = 7;` added to `RenderResponse.oneof chunk`
- [x] Generated Python bindings (fixed relative imports in `proto/*_pb2.py`)

### Phase 2: Server Implementation ✅
- [x] Create progress module structure
  - [x] Create [service/progress/](../service/progress/) folder
  - [x] Create [service/progress/__init__.py](../service/progress/__init__.py) - export ProgressTracker
  - [x] Create [service/progress/tracker.py](../service/progress/tracker.py) with:
    - [x] `__init__(emit_interval, is_online, total_frames)` - create and initialize
    - [x] `track_frame_progress(realtime_metric)` - returns metadata when ready
    - [x] `_create_metadata(realtime_metric)` - creates RenderMetadata with Status
    - [x] `_calculate_progress()` - compute percentage (offline mode only)
- [x] Integrate in [service/streaming.py](../service/streaming.py)
  - [x] Import: `from service.progress import ProgressTracker`
  - [x] Create tracker before `reader_thread` (line ~494)
  - [x] Pass tracker to `reader_thread`
  - [x] Set `total_frames` from audio duration in offline mode (line ~327-330)
  - [x] Call `tracker.track_frame_progress(realtime_metric)` after each video frame
  - [x] Yield metadata if returned
- [x] Fixed proto imports: `render_service_pb2_grpc.py` relative import
- [x] Offline mode: full audio in single chunk → `total_frames = int(chunk_sum * TARGET_FPS)`
- [x] Added `TARGET_FPS = 25` constant in streaming.py (single source of truth)

### Phase 3: Testing ✅
- [x] Update test client to display progress ([infra/local/test/test_client.py](../infra/local/test/test_client.py))
- [x] Manual testing via test_client.py (backward compatible - old clients ignore metadata)

---

## References

- [Google Cloud Speech API - Progress metadata example](https://github.com/googleapis/googleapis/blob/master/google/cloud/speech/v1/cloud_speech.proto)
- [Google Service Management API - Progress metadata example](https://github.com/googleapis/googleapis/blob/master/google/api/servicemanagement/v1/resources.proto)
- [Protocol Buffers Best Practices](https://protobuf.dev/programming-guides/dos-donts/)
