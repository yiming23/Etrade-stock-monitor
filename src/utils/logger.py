"""
Logging configuration using loguru.
"""

import sys
from pathlib import Path

from loguru import logger

from src.utils.config import PROJECT_ROOT


LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Remove default handler
logger.remove()

# Console output with color
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    level="INFO",
    colorize=True,
)

# File output with rotation
logger.add(
    LOG_DIR / "monitor_{time:YYYY-MM-DD}.log",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} - {message}",
    level="DEBUG",
    rotation="1 day",
    retention="30 days",
    compression="zip",
)


def get_logger(name: str = __name__) -> "logger":
    """Get a contextualized logger."""
    return logger.bind(name=name)
