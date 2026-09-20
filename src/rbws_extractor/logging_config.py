"""Structured logging setup."""

from __future__ import annotations

import logging
import sys
from typing import Optional


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure root logger once and return the package logger."""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger = logging.getLogger("rbws_extractor")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    if name:
        return logging.getLogger(f"rbws_extractor.{name}")
    return logging.getLogger("rbws_extractor")