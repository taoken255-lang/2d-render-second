# Alpha Segmentation Bugs

## Status: Dormant (feature unused)

## Overview

The `alpha=true` path has two bugs that cause undefined behavior. Currently dormant since no clients use `alpha=true`.

For how alpha works, see [docs/alpha.md](../docs/alpha.md).

## Bug 1: Channel Count Mismatch

### Location
`service/streaming.py` - `get_mqueue_thread()`

### Problem
Sends 3-channel RGB data to AlphaService which expects 4-channel BGRA.

```python
# Line 421 - WRONG: still 3 channels
bgra = (rgb[..., [0, 1, 2]]).tobytes()  # Just reorders RGB -> RGB (3 bytes/pixel)
frame.data = bgra

# alpha.py:25 - expects 4 bytes/pixel
Image.frombytes("RGBA", (width, height), frame_in.data, "raw", "BGRA")
```

### Impact
- Expected: `width * height * 4` bytes
- Actual: `width * height * 3` bytes
- Result: `ValueError: not enough image data`

### Fix
```python
# Line 421 - create actual BGRA with alpha=255
bgra = np.empty((frame.height, frame.width, 4), dtype=np.uint8)
bgra[..., 0] = rgb[..., 2]  # B
bgra[..., 1] = rgb[..., 1]  # G
bgra[..., 2] = rgb[..., 0]  # R
bgra[..., 3] = 255          # A
frame.data = bgra.tobytes()
```

## Bug 2: Wrong cv2 Color Conversion

### Location
`service/alpha.py` - `AlphaService.segment_person_from_pil_image()`

### Problem
Uses 3-channel conversion code on 4-channel image.

```python
# Line 25-27
pil_image = Image.frombytes("RGBA", ..., "raw", "BGRA")  # 4-channel RGBA
frame = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)  # WRONG: 3-channel code
```

### Fix
```python
frame = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGBA2BGR)  # Correct: 4->3 channel
```

## Related

- [docs/alpha.md](../docs/alpha.md) - How alpha works
- [docs/known_issues.md](../docs/known_issues.md)
