from loguru import logger

from logging_config import configure_logging


configure_logging()


def method():
    logger.info("hello")
