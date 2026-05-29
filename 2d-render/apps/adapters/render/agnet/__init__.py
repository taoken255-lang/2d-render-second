"""Agnet Adapter - Bridge to Agnet gRPC Render Service.

This adapter acts as an HTTP-to-gRPC bridge, translating HTTP requests from
the universal worker to gRPC calls to the Agnet render service.

Architecture Pattern: Bridge (NOT wrapper)
- Worker → HTTP → Adapter → gRPC → Agnet Service
- Adapter is stateless, CPU-only (no GPU needed)
- Agnet service is remote (separate container/process)

Key Responsibilities:
1. Accept HTTP render requests from worker (universal contract)
2. Preprocess images (resize, JPG conversion, validation)
3. Call Agnet gRPC API with preprocessed inputs
4. Collect frames from streaming gRPC response
5. Combine frames + audio into MP4 using ffmpeg
6. Upload result to S3 presigned URL
7. Provide temporary storage for last N jobs (fallback access)

Reference:
- Agnet API: /home/igor/repos/2d-render/docs/api.md
- Input requirements: /home/igor/repos/2d-render/docs/input.md
- Test logic: /home/igor/repos/2d-render/infra/local/test/api_test.sh
"""

__version__ = "0.1.0"
