import asyncio
import os
from pathlib import Path

import aiortc.codecs.h264

bitrate = int(os.getenv("BITRATE", 16_000_000))
setattr(aiortc.codecs.h264, "DEFAULT_BITRATE", bitrate)
setattr(aiortc.codecs.h264, "MAX_BITRATE", bitrate)
setattr(aiortc.codecs.h264, "MIN_BITRATE", bitrate)

from rtc_mediaserver.common.config import settings
from rtc_mediaserver.common.logging_config import setup_default_logging, get_logger

setup_default_logging()
logger = get_logger(__name__)

async def warm_up():
    if settings.offline_warmup:
        from rtc_mediaserver.render.offline.grpc_utils import local_video_run
        sample_rate = 16000
        bps = 16
        warmup_audio = b"\x00" * sample_rate * (bps // 8)  # 1 сек тишины s16le mono

        for avatar in settings.offline_avatars:
            await local_video_run(
                audio=warmup_audio,
                sample_rate=sample_rate,
                bps=bps,
                avatar_id=avatar,
                output_path=Path(f"offline_data/_warmup/{avatar}"),
                audio_fmt="s16le",
                tail_video_path=None,
            )

if __name__ == "__main__":  # pragma: no cover
    asyncio.run(warm_up())
    import uvicorn

    app_location = "rtc_mediaserver.api.main:app"

    if settings.https:
        logger.info(f"Starting secure Simple WebRTC Server → http://localhost:{settings.port}")
        uvicorn.run(
            app_location,
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
            app_location,
            host="0.0.0.0",
            port=settings.port,
            reload=False,
            access_log=True,
            loop="asyncio"
        )
