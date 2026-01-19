import asyncio
import os
import signal
import time

from rtc_mediaserver.logging_config import get_logger
from rtc_mediaserver.webrtc_server.info import info

logger = get_logger(__name__)

async def watchdog():
    t_s = time.time()
    logger.info(f"Watchdog started")

    while True:
        await asyncio.sleep(10)
        try:
            logger.info(f"Watchdog check status")
            await info()
            logger.info(f"Watchdog check status - OK")
            t_s = time.time()
        except:
            logger.info(f"Watchdog check status - FAILED")
            if time.time() - t_s >= 600:
                logger.info(f"Watchdog KILL CONTAINER")
                os._exit(1)