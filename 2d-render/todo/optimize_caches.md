# Optimize Video Avatar Caching (pkl)

## Goal

Cut **TTFF** (time to first frame) by eliminating the **7GB per-avatar cache load** bottleneck while keeping quality acceptable and code changes minimal.

## Current State (verified)

- Video avatar cache file is ~7GB mainly because of **`f_s_lst`** (appearance features) stored per source video frame.
- On some disks/volumes this causes **~60s+ cache read** per request (plus deserialize time).
- `img_rgb_lst` is **already not stored** for video avatars (video frames stream from file) - the real problem is `f_s_lst`.

## Quick Wins (implement now)

### 1) Async cache save (remove ~10s+ block on first request)

**Problem:** First request computes cache, then blocks waiting to write it to disk - but the request uses the in-memory cache anyway.

**Fix:** Save cache on a background thread; return immediately.

```python
# avatar_registrar.py (after building source_info)
logger.info("CACHE CREATED, SAVING (background)")
threading.Thread(
    target=self._save_cache,
    args=(cache_path, source_info),
    daemon=True
).start()

# return source_info immediately (in-memory)
return source_info
```

**Trade-off:** If a second request arrives during save, it may recompute once (acceptable).

---

## Root Cause (why 7GB)

For video avatars, the cache stores per-source-frame tensors:

- `f_s` shape ~ `[1, 32, 16, 64, 64]` float16
- bytes per frame = `1*32*16*64*64*2 = 4,194,304` bytes ≈ **4.0 MiB**
- for 1680 frames: `4.0 MiB * 1680 ≈ 6.6 GiB` (plus overhead) => ~7GB pkl

This is why disk read dominates.

## Core Fix (RECOMMENDED)

### 2) Split cache into: small header + lazy `f_s` store (no upfront 7GB read)

**Idea:** Do **not** load all `f_s` at request start.
- Load a small **meta/header** (fast).
- Keep `f_s` in a **streaming-friendly format** and load only frames you need (or keyframes).
- Prefetch next frames/chunks in background.

#### New cache layout (backwards-compatible)

```
cache/<avatar_id>/<ver>/<hash>/
  meta.json                # tiny (fps, num_frames, dims, keys present)
  x_s_info.pkl             # small (or npz)
  M_c2o.npy                # small
  eye_open.npy             # tiny
  eye_ball.npy             # tiny
  f_s.dat                  # BIG, float16, memmap OR sharded chunks
```

`meta.json` example:
```json
{
  "format": "ditto-cache-v2",
  "num_source_frames": 1680,
  "fs_shape": [1680, 32, 16, 64, 64],
  "fs_dtype": "float16",
  "fs_layout": "memmap",
  "key_frame_interval": 25
}
```

#### Storage options for `f_s` (pick one)

**Option D1 - Memmap (best TTFF, simplest runtime)**
- Write `f_s` as one contiguous float16 array: shape `[T, 32, 16, 64, 64]`
- Load via `numpy.memmap` - instant open, pages in on demand.

Pros: zero upfront read, good sequential read.
Cons: needs stable local filesystem; docker overlay / network mounts can be slow.

**Option D2 - Sharded chunks (best portability)**
- Store `f_s` in shard files, e.g. 25 frames per shard (`~100 MiB` each).
- Load only required shard(s).

Pros: works well on any FS, easier to distribute.
Cons: slightly more code.

#### Runtime API (KISS boundary)

Add one small abstraction:

```python
class FsStore:
    def get(self, frame_idx: int): ...
    def prefetch(self, frame_idx: int, n: int): ...
```

`StreamSDK` uses:
```python
f_s = self.fs_store.get(frame_idx)
```

No other code should care whether it came from memmap, shard, or keyframe.

---

## Size Reduction Options (optional, combine with Core Fix)

### Option A: Key-frame `f_s` (recommended after Core Fix)

Store `f_s` only for keyframes (every Nth source frame), then use nearest keyframe:

| Keyframe interval | Stored frames | f_s size | Typical load |
|---:|---:|---:|---:|
| 1 (current) | 1680 | ~6.6 GiB | huge |
| 10 | 168 | ~660 MiB | manageable |
| 25 | 67 | ~260 MiB | fast |

Runtime: `key = (frame_idx // N) * N`, then `f_s = f_s_key[key]`.

Trade-off: can degrade quality on fast head turns between keyframes.

### Option B: Single `f_s` (aggressive)

Use one representative `f_s` for all frames.
- Small cache
- Best TTFF
- Worst quality if source video has big pose changes

### Option C: On-demand extraction (no f_s cache)

Do not store `f_s`; compute it when needed.
- Great disk usage
- Adds compute per unique source frame

---

## Implementation Plan (concrete)

### Phase 0 - logging (already done)
Keep `avatar_cache_read` vs `avatar_cache_deserialize` timings - this proves wins.

### Phase 1 - introduce cache v2 format (no behavior change yet)
1. Write `meta.json` alongside pkl.
2. Add versioning: `format=ditto-cache-v2`.

### Phase 2 - move `f_s` out of pkl
1. While creating cache:
   - stack `f_s` into one tensor/ndarray (`[T, 32, 16, 64, 64]`)
   - write as memmap `f_s.dat` (or shard files)
2. In pkl keep only small lists (`x_s_info_lst`, `M_c2o_lst`, etc) OR convert them too.
3. On load:
   - load meta + small data
   - create `FsStore` that points to memmap/shards
   - DO NOT read all f_s into RAM

### Phase 3 - optional keyframes
1. During cache creation, compute/store only keyframe `f_s`.
2. Update `FsStore.get()` to map frame -> nearest keyframe.

---

## Files to Modify

- `agnet/core/atomic_components/avatar_registrar.py`
  - Write cache v2: `meta.json`
  - Move `f_s` to `f_s.dat` (memmap) or shard files
  - Keep async save

- `agnet/stream_pipeline_online.py`
  - Replace `self.source_info["f_s_lst"][frame_idx]` with `self.fs_store.get(frame_idx)`
  - Add prefetch hook (optional)

- (new) `agnet/cache/fs_store.py`
  - `MemmapFsStore`
  - `ShardedFsStore`
  - `KeyframeFsStore` wrapper (optional)

---

## Testing Checklist

1. Delete old pkl cache for one avatar.
2. Run once (cache miss) - verify TTFF improves because save is async.
3. Run again (cache hit) - verify:
   - `avatar_cache_read` drops dramatically (no 7GB read)
   - first frame quality is unchanged (baseline)
4. Stress: 2 concurrent requests same avatar:
   - no crashes
   - acceptable recompute if save race

---

## Notes / Risks

- If cache directory is on slow network storage, memmap may still be slow but it avoids upfront full-file read.
- For docker: prefer a bind mount on fast local disk for cache directory (avoid overlay filesystem).
- Keep backwards compatibility: if `meta.json` missing, fall back to old pkl loader.
