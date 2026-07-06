"""Run every collector in sequence with per-step isolation (shared by CLI and weekly)."""

from __future__ import annotations

from collections.abc import Callable

import duckdb

from moi.config import get_settings
from moi.logging import get_logger
from moi.universe import sync_universe

log = get_logger(__name__)


def collect_everything(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """Run all collectors; a failing step is reported, not fatal. Returns (name, outcome)."""
    from moi.ingest.congress import collect_congress
    from moi.ingest.edgar_13f import collect_13f
    from moi.ingest.edgar_form4 import collect_form4
    from moi.ingest.macro import collect_macro
    from moi.ingest.news import collect_news
    from moi.ingest.polymarket import collect_polymarket
    from moi.ingest.prices import collect_prices

    settings = get_settings()
    sync_universe(con)

    steps: list[tuple[str, Callable[[], int]]] = [
        ("prices", lambda: collect_prices(con, years=settings.price_history_years)),
        ("13f", lambda: collect_13f(con)),
        ("form4", lambda: collect_form4(con)),
        ("congress", lambda: collect_congress(con)),
        ("polymarket", lambda: collect_polymarket(con)),
        ("news", lambda: collect_news(con)),
        ("macro", lambda: collect_macro(con)),
    ]
    results: list[tuple[str, str]] = []
    for name, fn in steps:
        try:
            results.append((name, f"ok ({fn()} rows)"))
        except Exception as exc:
            log.error("collect_step_failed", step=name, error=str(exc))
            results.append((name, f"ERROR: {exc}"))
    return results
