import asyncio
import logging
import time

from rtc_mediaserver.webrtc_server.info import info


async def render_watchdog():
    logging.error(f"Watchdog checking 2d render status - STARTED")
    t_start = time.time()
    while True:
        await asyncio.sleep(10)
        try:
            logging.error(f"Watchdog checking 2d render status")
            await info()
            t_start = time.time()
            logging.error(f"Watchdog checking 2d render status -> SUCCESS")
        except:
            logging.error(f"Watchdog checking 2d render status -  DEAD")
            if time.time() - t_start > 30:
                logging.error(f"Watchdog - kill process")
                exit(-1)
