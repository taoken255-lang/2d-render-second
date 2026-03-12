# Width/Height Swap Bug

## Status: Needs Verification

## Location

`service/streaming.py` - `get_mqueue_thread()`

## Problem

Numpy reshape uses `(width, height)` instead of `(height, width)` for image arrays.

### Affected Code

```python
# Line 419 - WRONG
rgb = np.frombuffer(frame.data.tobytes(), dtype=np.uint8).reshape(frame.width, frame.height, 3)

# Line 436 - WRONG
bgra = np.empty((frame.width, frame.height, 4), dtype=np.uint8)

# Line 451 - CORRECT (inconsistent!)
alpha = np.full((frame.height, frame.width, 1), 255, dtype=np.uint8)
```

## Expected Behavior

Numpy image arrays should be `(height, width, channels)` - rows x columns x channels.

## Impact

For **square frames** (e.g., 512x512): No visible issue - width == height.

For **non-square frames** (e.g., 1280x720):
- Reshape succeeds (total bytes match: 1280x720x3 = 720x1280x3)
- Image is **transposed/corrupted**

## Verification Needed

Current avatar video is 1280x720 (non-square). If system works correctly in production:
1. SDK may output data in width-major order (compensates)
2. Client may expect transposed format
3. May only be tested with square frames

**Test**: Run with non-square avatar, verify output visually.

## Fix

```python
# Line 419
rgb = np.frombuffer(frame.data.tobytes(), dtype=np.uint8).reshape(frame.height, frame.width, 3)

# Line 436
bgra = np.empty((frame.height, frame.width, 4), dtype=np.uint8)
```

## Related

- [docs/known_issues.md](../docs/known_issues.md)
- [docs/animation_system.md](../docs/animation_system.md)
