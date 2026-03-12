# Optimize Librosa First-Call Delay

## Problem

First call to `librosa.load()` takes **~11 seconds** due to lazy initialization of audio backends.
Subsequent calls take <1ms.

```
[TIMING] [AUDIO] decode_ms=11179.9 chunk_bytes=48000 samples=16000  ← FIRST: 11.2s
[TIMING] [AUDIO] decode_ms=0.8 chunk_bytes=48000 samples=16000      ← SECOND: 0.8ms
```

This adds ~11s to `prime_first_frame` on first request after container start.

## Root Cause

`librosa.load()` lazily initializes on first call:
- FFmpeg/audioread backend detection and loading
- NumPy/SciPy signal processing imports
- Resampling filter coefficient computation

## Current Code Path

```python
# render.py:157-169
wav_buffer = io.BytesIO()
with wave.open(wav_buffer, 'wb') as wav_file:
    wav_file.setnchannels(1)
    wav_file.setsampwidth(2)
    wav_file.setframerate(frame_rate)
    wav_file.writeframes(audio_chunk)
wav_buffer.seek(0)

audio, sr = librosa.load(wav_buffer, sr=16000)  # ← 11s first call
```

## Solutions (Priority Order)

### Option 1: Warmup at Service Init (Quick Win)

Add librosa warmup during container startup (one-time cost).

**Location:** `server.py` or `RenderService.__init__()`

```python
import librosa
import numpy as np
import io
import wave

def warmup_librosa():
    """Pre-load librosa backends to avoid first-call delay."""
    dummy_audio = np.zeros(16000, dtype=np.int16).tobytes()  # 1s silence
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(dummy_audio)
    wav_buffer.seek(0)
    librosa.load(wav_buffer, sr=16000)

# Call during startup
warmup_librosa()
```

**Impact:** Moves ~11s from TTFF to service startup (acceptable).

### Option 2: Skip Librosa Entirely (Best Performance)

Client sends 16kHz 16-bit PCM. No need to encode→decode→resample.

**Current flow (wasteful):**
```
Client: raw PCM → encode WAV → send
Server: receive → decode WAV → resample → process
```

**Optimized flow:**
```
Client: raw PCM → send
Server: receive → convert to float32 → process
```

**Replace librosa.load() with:**
```python
# Direct PCM to float32 conversion (no librosa needed)
audio = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32) / 32768.0
```

**Requirements:**
- Client must send raw 16kHz 16-bit mono PCM (verify proto format)
- Update frame_rate handling if needed

**Impact:** Eliminates librosa entirely, ~11s saved + lower CPU per chunk.

### Option 3: Use Faster Audio Library

Replace librosa with lighter alternatives:
- `soundfile` - faster, fewer dependencies
- `scipy.io.wavfile` - built-in, no FFmpeg
- `torchaudio` - GPU-accelerated if needed

```python
import soundfile as sf
audio, sr = sf.read(wav_buffer)
if sr != 16000:
    audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
```

## Implementation Plan

1. **Phase 1 (Quick):** Add warmup to server.py startup
   - Verify TTFF drops by ~11s on first request
   - Monitor service startup time increase

2. **Phase 2 (Optimal):** Investigate direct PCM path
   - Check proto definition for audio format
   - Test with raw PCM if client supports it
   - Remove WAV encode/decode round-trip

## Testing

```bash
# Before: first request after container start
[TIMING] [AUDIO] decode_ms=11179.9  # BAD

# After warmup: first request
[TIMING] [AUDIO] decode_ms=0.8      # GOOD

# Verify TTFF improvement
prime_first_frame: 12s → ~1s (after warmup moves to init)
```

## Files to Modify

- `server.py` - add warmup call at startup
- `service/render.py` - optimize audio conversion (Phase 2)
- `docs/monitoring/timings.md` - update TTFF breakdown

## Related

- [Timing Investigation](../monitoring/timings.md)
- [Ditto Paper](https://arxiv.org/html/2411.19509v2) - claims 385ms FFD
