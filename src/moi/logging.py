"""Structured logging setup. Call `configure_logging()` once at process start."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", *, json: bool = False) -> None:
    """Configure structlog + stdlib logging.

    Args:
        level: Root log level name (e.g. "INFO", "DEBUG").
        json: Emit JSON lines (for cron/file capture) instead of console-friendly output.
    """
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
