# Support Progress Tracking from Agnet gRPC Service

## Problem

Agnet server already sends `RenderMetadata` with `progress_percent` (0-100).
Adapter had old proto - **ignored this field silently**.

## Solution

1. Updated proto to include `metadata = 7` field
2. Handle metadata in grpc_client.py response loop
3. Removed input-chunk estimation (caused progress jump-back)

## Implementation

**grpc_client.py** - handle metadata in response loop:

```python
elif which == 'metadata':
    status = response.metadata.status
    if status.HasField('progress_percent') and on_progress:
        on_progress(status.progress_percent / 100.0)
```

**No changes to runner.py** - existing callback interface works.

## Checklist

- [x] Generate proto bindings from 2d-render
- [x] Fix relative imports in generated files
- [x] Handle `metadata` in response loop
- [x] Remove input-chunk estimation (prevents progress jump-back)
- [ ] Test with Agnet server
