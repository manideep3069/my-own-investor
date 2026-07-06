"""Price normalization and idempotent upsert."""

from __future__ import annotations

from datetime import date

import pandas as pd

from moi.ingest.prices import PriceRow, normalize_yf_frame, upsert_prices


def _frame() -> pd.DataFrame:
    idx = pd.to_datetime(["2026-01-02", "2026-01-05"])
    return pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [10.5, 11.5],
            "Low": [9.5, 10.5],
            "Close": [10.2, 11.2],
            "Adj Close": [10.1, 11.1],
            "Volume": [1000, 2000],
        },
        index=idx,
    )


def test_normalize_yf_frame() -> None:
    rows = normalize_yf_frame("ALAB", _frame())
    assert len(rows) == 2
    assert rows[0].ticker == "ALAB"
    assert rows[0].date == date(2026, 1, 2)
    assert rows[0].close == 10.2
    assert rows[0].adj_close == 10.1
    assert rows[0].volume == 1000
    assert rows[0].source == "yfinance"


def test_normalize_empty_frame() -> None:
    assert normalize_yf_frame("ALAB", pd.DataFrame()) == []


def test_upsert_is_idempotent(db) -> None:
    rows = normalize_yf_frame("ALAB", _frame())
    assert upsert_prices(db, rows) == 2
    # Re-running the same rows must not create duplicates.
    upsert_prices(db, rows)
    count = db.execute("SELECT count(*) FROM prices_daily").fetchone()[0]
    assert count == 2

    # An updated close for an existing (ticker, date) overwrites, not appends.
    changed = [PriceRow(**{**rows[0].__dict__, "close": 99.0})]
    upsert_prices(db, changed)
    count = db.execute("SELECT count(*) FROM prices_daily").fetchone()[0]
    close = db.execute(
        "SELECT close FROM prices_daily WHERE ticker='ALAB' AND date='2026-01-02'"
    ).fetchone()[0]
    assert count == 2
    assert close == 99.0
