# Fixed: Missing Frame Size Metadata for Avatar-based Renders

**Status:** COMPLETED (2026-01-21)

---

## Issue

VideoChunk responses were missing `width` and `height` fields when using pre-configured avatars (`avatar_id` mode), causing downstream FFmpeg encoding failures in `async-2d-render` adapter.

**Error:**
```python
expected_frame_size = frame_width * frame_height * channels
TypeError: unsupported operand type(s) for *: 'NoneType' and 'NoneType'
```

**Affected modes:**
- [OK] Custom image mode (using `ImageChunk`)
- [FAIL] Avatar mode (using `SetAvatar` with `avatar_id`)

---

## Root Cause

Dimensions were extracted correctly from the MP4 metadata and passed to `stream_frames_thread`, but there was no mechanism to track dimensions at the `RenderStream` level as a fallback when `frame.width`/`frame.height` might be `None`.

---

## Solution Implemented

Added dimension tracking via internal events and fallback logic in `VideoChunk` creation.

### Changes to `service/streaming.py`

#### 1. Added dimension tracking variables in `RenderStream` (line 640-641)

```python
# Track frame dimensions for VideoChunk (fallback if frame.width/height is None)
tracked_width = None
tracked_height = None
```

#### 2. Enhanced `avatar_set` event to include dimensions (line 343)

```python
# Before
video_queue.put(EventObject(event_name="avatar_set", event_data={"avatar_id": avatar_name}))

# After
video_queue.put(EventObject(event_name="avatar_set", event_data={"avatar_id": avatar_name, "width": img_width, "height": img_height}))
```

#### 3. Added `image_set` event for image mode (line 276)

```python
video_queue.put(EventObject(event_name="image_set", event_data={"width": img_width, "height": img_height}))
```

#### 4. Track dimensions from events (lines 693-706)

```python
elif frame.event_name == "avatar_set":
    avatar_sent_mem = frame
    avatar_id = frame.event_data.get("avatar_id")
    # Track dimensions from avatar_set event (avatar mode)
    if frame.event_data.get("width") is not None and frame.event_data.get("height") is not None:
        tracked_width = frame.event_data.get("width")
        tracked_height = frame.event_data.get("height")
        logger.debug(f"[DIMENSION] Tracked avatar dimensions from event: {tracked_width}x{tracked_height}")
elif frame.event_name == "image_set":
    # Track dimensions from image_set event (image mode)
    if frame.event_data.get("width") is not None and frame.event_data.get("height") is not None:
        tracked_width = frame.event_data.get("width")
        tracked_height = frame.event_data.get("height")
        logger.debug(f"[DIMENSION] Tracked image dimensions from event: {tracked_width}x{tracked_height}")
```

#### 5. Fallback logic in VideoChunk creation (lines 762-773)

```python
# Track dimensions from first valid frame, use as fallback for subsequent frames
if frame.width is not None and frame.height is not None:
    if tracked_width is None:
        tracked_width = frame.width
        tracked_height = frame.height
        logger.debug(f"[DIMENSION] Tracked frame dimensions: {tracked_width}x{tracked_height}")
# Use tracked dimensions as fallback if frame dimensions are None
video_width = frame.width if frame.width is not None else tracked_width
video_height = frame.height if frame.height is not None else tracked_height
if video_width is None or video_height is None:
    logger.warning(f"[DIMENSION] Missing frame dimensions at frame_idx={frame_idx}")
yield RenderResponse(video=VideoChunk(data=frame.data, width=video_width or 0, height=video_height or 0, frame_idx=frame_idx))
```

#### 6. Added dimension logging (lines 269, 325)

```python
# Image mode
logger.info(f"[DIMENSION] Image mode dimensions: {img_width}x{img_height}")

# Avatar mode
logger.info(f"[DIMENSION] Avatar mode dimensions from MP4: {img_width}x{img_height}")
```

---

## Verification

Tested with `AVATAR_ID=ermakova` and confirmed dimensions flow correctly:

```
[DIMENSION] Avatar mode dimensions from MP4: 1920x1076
[DIMENSION] Tracked avatar dimensions from event: 1920x1076
[EVENT] first_frame_ready frame_idx=0 realtime_metric=0.000s 1920x1076
[MQUEUE] bytes=6197760 expected=6197760 w=1920 h=1076
```

Math: `1920 × 1076 × 3 = 6,197,760 bytes` ✓

---

## Backward Compatibility

Fully backward compatible:
- Proto schema unchanged
- Frame data untouched
- Clients see properly populated `width`/`height` (was missing before)
- No client code changes required

---

## Files Modified

| File | Changes |
|------|---------|
| `service/streaming.py` | Added dimension tracking, events, fallback logic, logging |

---

## References

- Original issue: [todo/fix_size_metadata_avatar_id.md](../fix_size_metadata_avatar_id.md)
- Proto definition: [protobuf/render_service.proto](../../protobuf/render_service.proto)
