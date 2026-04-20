import asyncio
import os
import time

from rtc_mediaserver.common.logging_config import get_logger
from rtc_mediaserver.render.common.info import info

logger = get_logger(__name__)

async def watchdog():
    t_s = time.time()
    logger.info(f"Watchdog started")

    while True:
        await asyncio.sleep(10)
        try:
            logger.debug(f"Watchdog check status")
            await info()
            logger.debug(f"Watchdog check status - OK")
            t_s = time.time()
        except:
            logger.warning(f"Watchdog check status - FAILED")
            if time.time() - t_s >= 30:
                logger.warning(f"Watchdog KILL CONTAINER")
                os._exit(1)