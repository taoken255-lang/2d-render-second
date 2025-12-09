#!/usr/bin/env python3
"""Compatibility wrapper around the refactored WebRTC server package.

The actual implementation now lives under ``rtc_mediaserver.webrtc_server``.
This file is kept to avoid breaking existing ``uvicorn`` invocation paths such as

    uvicorn simple_webrtc_server:app --host 0.0.0.0 --port 8080

Feel free to import ``rtc_mediaserver.webrtc_server`` directly in new code.
"""
import os

import aiortc.codecs.h264
bitrate = int(os.getenv("BITRATE", 16_000_000))
setattr(aiortc.codecs.h264, "DEFAULT_BITRATE", bitrate)
setattr(aiortc.codecs.h264, "MAX_BITRATE", bitrate)
setattr(aiortc.codecs.h264, "MIN_BITRATE", bitrate)

from rtc_mediaserver.config import settings
from rtc_mediaserver.logging_config import setup_default_logging, get_logger

# Configure logging using unified formatter
setup_default_logging()
logger = get_logger(__name__)

# Re-export FastAPI application instance
from rtc_mediaserver.webrtc_server import app  # noqa: E402  (import after logging setup)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    if settings.https:
        logger.info(f"Starting secure Simple WebRTC Server → http://localhost:{settings.port}")
        uvicorn.run(
            "rtc_mediaserver.webrtc_server.api:app",
            host="0.0.0.0",
            port=settings.port,
            reload=False,
            access_log=True,
            ssl_keyfile=settings.ssl_key,
            ssl_certfile=settings.ssl_cert,
            loop="asyncio"
        )
    else:
        logger.info(f"Starting Simple WebRTC Server → http://localhost:{settings.port}")
        uvicorn.run(
            "rtc_mediaserver.webrtc_server.api:app",
            host="0.0.0.0",
            port=settings.port,
            reload=False,
            access_log=True,
            loop="asyncio"
        )
