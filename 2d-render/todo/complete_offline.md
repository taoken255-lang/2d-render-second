# TODO: Complete Offline Mode Implementation

## CRITICAL
Offline was working in the worse quality compared with online. Need to investigate deeper before further implementation!

## Issue Summary

Offline mode (`online=false`) is currently broken. When users set `online=false`, they incorrectly receive the same streaming behavior as online mode instead of batch processing.

**Root Cause**: Bug introduced on July 31, 2025 (commit 9e4291e) - offline mode was accidentally disabled during bug fixes.

## Git History

```
3ebfa59 (Jul 30): "integrate fully offline mode" → Working ✓
dd6f26c (Jul 31): "small fix" → Broke it by using onlineSDK
2e78e21 (Jul 31): "return offline render" → Attempted fix
9e4291e (Jul 31): "return online to offline" → Left broken
e8438cb (Oct 13): "ref objects fix killcontainer" → Removed import
```

## Current Behavior vs Expected

| Aspect | Current (Bug) | Expected |
|--------|--------------|----------|
| SDK Used | `onlineSDK` for both | `offlineSDK` for offline |
| Method Called | `render_chunk_online` | `render_chunk_offline` |
| Audio Processing | Streaming chunks | Accumulate then process |
| Buffer | Pre-allocated (online) | Dynamic growth (offline) |

## Files Requiring Changes

### 1. `service/render.py`

**Location**: Lines 33-34

**Current Code** (BROKEN):
```python
if is_online:
    self.sdk = onlineSDK(cfg_pkl, data_root)
    self.render_chunk = self.render_chunk_online
else:
    self.sdk = onlineSDK(cfg_pkl, data_root)          # ← BUG
    self.render_chunk = self.render_chunk_online      # ← BUG
```

**Fixed Code**:
```python
if is_online:
    self.sdk = onlineSDK(cfg_pkl, data_root)
    self.render_chunk = self.render_chunk_online
else:
    self.sdk = offlineSDK(cfg_pkl, data_root)         # ← FIX
    self.render_chunk = self.render_chunk_offline     # ← FIX
```

**Location**: Line 10 (imports)

**Current Code** (MISSING IMPORT):
```python
from agnet.stream_pipeline_online import StreamSDK as onlineSDK
```

**Fixed Code**:
```python
from agnet.stream_pipeline_online import StreamSDK as onlineSDK
from agnet.stream_pipeline_offline import StreamSDK as offlineSDK  # ← ADD THIS
```

## Implementation Details

### Classes/Methods Involved

**service/render.py**:
- `RenderService.__init__` - Initialize with correct SDK based on mode
- `RenderService.render_chunk_online` - Currently used for both modes (line 139-185)
- `RenderService.render_chunk_offline` - Exists but unused (line 121-138)

**agnet/stream_pipeline_online.py**:
- `StreamSDK` - Used for real-time streaming

**agnet/stream_pipeline_offline.py**:
- `StreamSDK` - Implements batch processing (complete implementation exists)
- `StreamSDK._audio2motion_offline` - Processes accumulated audio

**service/streaming.py**:
- `StreamingService.RenderStream` - Receives `online` parameter and passes to render process (line 471)
- `start_render_process` - Creates `RenderService` with mode (line 207)
- `render_stream` - Calls `render.render_chunk()` (line 131)

### How It Works

1. Client sends `RenderRequest` with `online=false`
2. `StreamingService.RenderStream` reads parameter (streaming.py:315, 353)
3. Passes mode to `start_render_process` via queue (streaming.py:207)
4. `RenderService.__init__` should initialize correct SDK (render.py:33)
5. Audio chunks call `render.render_chunk()` which should be `render_chunk_offline`
6. `render_chunk_offline` accumulates audio, processes on `is_last=True`

## Testing After Fix

### Test Cases

**Test 1: Offline Mode Behavior**
```python
# In test_client.py or new test
request = RenderRequest(
    image=ImageChunk(data=..., width=..., height=...),
    online=False,  # Offline mode
    output_format="RGB"
)
# Should use offlineSDK and accumulate audio
```

**Test 2: Both Modes Work**
```bash
cd infra/local
make test render  # Test offline (default)
make test online  # Test online mode
```

**Expected Results**:
- Offline: All audio accumulated before processing starts
- Online: Frames generated as audio arrives
- Both: Produce valid output video

### Validation Points

1. Check logs show correct SDK initialization
2. Verify `render_chunk_offline` is called for offline mode
3. Confirm audio accumulation happens (buffer grows)
4. Ensure final video quality matches expectations
5. Compare performance: offline should be different latency profile

## Additional Notes

- **Backward Compatibility**: Fix is transparent to clients, no API changes needed
- **Documentation**: Already updated in docs/api.md and docs/known_issues.md
- **Existing Code**: `render_chunk_offline` implementation already complete
- **Risk**: Low - just wiring up existing code that was accidentally disabled

## Action Items

- [ ] Add import for `offlineSDK` (render.py line 10)
- [ ] Update line 33 to use `offlineSDK`
- [ ] Update line 34 to use `render_chunk_offline`
- [ ] Test both modes work correctly
- [ ] Verify audio accumulation in offline mode
- [ ] Update any additional documentation as needed
- [ ] Create commit: "fix: restore offline mode batch processing (render_chunk_offline)"

## References

- Bug location: [service/render.py:33-34](../service/render.py#L33-L34)
- Offline SDK: [agnet/stream_pipeline_offline.py](../agnet/stream_pipeline_offline.py)
- Offline method: [service/render.py:121-138](../service/render.py#L121-L138)
- Streaming entry: [service/streaming.py:207](../service/streaming.py#L207)
- Known issues: [docs/known_issues.md](../docs/known_issues.md)
