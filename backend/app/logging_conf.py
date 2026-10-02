"""Logging setup shared by the bot worker, the API and the CLI."""

from __future__ import annotations

import logging
import sys

from app.config import get_settings


def setup_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    # the HTTP client logs every request URL - which would contain the bot token
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(max(level, logging.INFO))
