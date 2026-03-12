# Fix: Dimension Tracking Bug for Portrait Images

## Issue

Portrait images taller than 1920px fail with reshape error during output processing.

**Error message:**
```
ERROR | GOT EXCEPTION IN GET MQUEUE THREAD cannot reshape array of size 7338240 into shape (1920,2892,3)
```

**Example:**
- Input: 1920x2892 JPG (portrait)
- Expected behavior: Auto-resize to 1275x1920, process, output
- Actual behavior: Resized to 1275x1920 but dimension tracking stays 1920x2892, reshape fails

---

## Root Cause

**Location:** service/streaming.py:141-144

```python
if int(Config.MAX_SIZE) < width:  # BUG: Only checks width!
    new_width = int(Config.MAX_SIZE)
    new_height = int(round(height * int(Config.MAX_SIZE) / width))
    height = new_height
```

**Problem:**
1. `check_resize` (loader.py:16-39) correctly resizes based on max(width, height)
2. But `stream_frames_thread` only checks if width > MAX_SIZE
3. For 1920x2892: width=1920 (not > 1920), so condition fails
4. Height (2892) is never checked
5. Dimension tracking stays at 1920x2892 while actual data is 1275x1920
6. Later reshape at streaming.py:402 fails

**Code flow:**
1. Image arrives: 1920x2892
2. check_resize (loader.py:16-39): 1920x2892 -> 1275x1920 (data resized)
3. stream_frames_thread (streaming.py:141-144): width=1920 not > 1920, no update
4. Dimension tracking: 1920x2892 (wrong!)
5. Output reshape (streaming.py:402): tries to reshape 7,338,240 bytes to (1920,2892,3) -> FAIL

---

## Fix Plan

### Option 1: Fix stream_frames_thread dimension check (streaming.py:141-144)

**Current code:**
```python
if int(Config.MAX_SIZE) < width:
    new_width = int(Config.MAX_SIZE)
    new_height = int(round(height * int(Config.MAX_SIZE) / width))
    height = new_height
```

**Fixed code:**
```python
if int(Config.MAX_SIZE) < max(width, height):
    if height > width:
        new_height = int(Config.MAX_SIZE)
        new_width = int(round(width * int(Config.MAX_SIZE) / height))
    else:
        new_width = int(Config.MAX_SIZE)
        new_height = int(round(height * int(Config.MAX_SIZE) / width))
    height = new_height
    width = new_width
```

**Impact:**
- Low risk: mirrors existing check_resize logic
- Fixes portrait images >1920px
- Maintains backward compatibility for landscape/square images

### Option 2: Remove duplicate resize logic

**Analysis:**
- check_resize already handles resizing correctly at loader.py
- stream_frames_thread resize check is redundant
- Could remove this check entirely and get dimensions from actual frame data

**Implementation:**
1. Remove resize check from stream_frames_thread
2. Get actual dimensions from frame data after neural network processing
3. Update ImageObject with actual dimensions

**Impact:**
- Medium risk: changes dimension flow
- More robust (dimensions come from actual data)
- Requires testing all code paths

---

## Recommended Approach

**Fix streaming.py:141-148** (Option 1) - low risk, targeted fix

```python
# Replace lines 141-144 with:
if int(Config.MAX_SIZE) < max(width, height):
    if height > width:
        # Portrait: height is limiting dimension
        new_height = int(Config.MAX_SIZE)
        new_width = int(round(width * int(Config.MAX_SIZE) / height))
    else:
        # Landscape or square: width is limiting dimension
        new_width = int(Config.MAX_SIZE)
        new_height = int(round(height * int(Config.MAX_SIZE) / width))
    height = new_height
    width = new_width
```

---

## Testing

**Test cases:**
1. Portrait >1920px: 1920x2892 (original failing case)
2. Portrait <1920px: 1080x1920 (should work already)
3. Landscape >1920px: 2892x1920 (should work already)
4. Square >1920px: 2160x2160 (test both dimensions)
5. Square <1920px: 1024x1024 (should work already)

**Validation:**
- Check logs for "Frame ready from NN" with correct dimensions
- Verify no reshape errors
- Confirm output video matches input aspect ratio

---

## Workaround (until fixed)

**Client-side preprocessing:**
```python
from PIL import Image

def preprocess_image(image_path, max_dim=1920):
    img = Image.open(image_path)
    w, h = img.size

    if max(w, h) > max_dim:
        if h > w:
            new_h = max_dim
            new_w = int(w * max_dim / h)
        else:
            new_w = max_dim
            new_h = int(h * max_dim / w)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    return img
```

**Usage:**
```python
img = preprocess_image("portrait_1920x2892.jpg")
img.save("portrait_1275x1920.jpg", quality=90)
# Send processed image to service
```

---

## References

- Bug location: service/streaming.py:141-144, 402
- Correct resize logic: agnet/core/atomic_components/loader.py:16-39 (check_resize)
- Error logs: "cannot reshape array of size 7338240 into shape (1920,2892,3)"
- Related: docs/known_issues.md, docs/input.md
