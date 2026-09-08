"""Loguru logging setup for the application."""

from __future__ import annotations

import sys

from loguru import logger


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru: remove default handler, add stderr with the given level.

    Calls are idempotent; repeated invocations just re-add a handler.
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan> | <level>{message}</level>",
        colorize=True,
    )
