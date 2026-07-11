"""Shared httpx client factory for collectors: timeout + connect retries."""

from __future__ import annotations

from typing import Any

import httpx


def client(timeout: float = 30, **kwargs: Any) -> httpx.Client:
    """An httpx.Client that retries transient connection failures (not HTTP errors)."""
    return httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(retries=2), **kwargs)
