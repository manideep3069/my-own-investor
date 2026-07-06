"""Backtest engine: perfect foresight wins, costs hurt, metrics sane."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from moi.backtest.engine import BacktestConfig, performance_metrics, run_backtest


@pytest.fixture()
def price_db(db):
    """DB with synthetic daily prices: WIN trends up hard, LOSE trends down."""
    dates = pd.bdate_range("2024-01-01", periods=400)
    n = len(dates)
    prices = {
        "WIN": np.linspace(100, 300, n),
        "LOSE": np.linspace(100, 60, n),
        "FLAT": np.full(n, 100.0) + np.sin(np.arange(n) / 7),
        "SPY": np.linspace(100, 120, n),
        "SMH": np.linspace(100, 130, n),
    }
    for ticker, px in prices.items():
        db.executemany(
            "INSERT INTO prices_daily (ticker, date, close, adj_close, volume, source) "
            "VALUES (?, ?, ?, ?, 1000, 'test')",
            [(ticker, d.date(), float(p), float(p)) for d, p in zip(dates, px, strict=True)],
        )
        db.execute(
            "INSERT OR REPLACE INTO universe (ticker, is_benchmark, active) VALUES (?, ?, TRUE)",
            [ticker, ticker in ("SPY", "SMH")],
        )
    return db


def _predictions(weeks: pd.DatetimeIndex) -> pd.DataFrame:
    rows = []
    for week in weeks:
        for ticker, score in (("WIN", 1.0), ("FLAT", 0.0), ("LOSE", -1.0)):
            rows.append({"ticker": ticker, "week_end": week, "score": score, "label": np.nan})
    return pd.DataFrame(rows)


def test_perfect_foresight_beats_universe(price_db, monkeypatch) -> None:
    import moi.backtest.engine as eng

    monkeypatch.setattr(eng, "candidate_tickers", lambda: ["WIN", "LOSE", "FLAT"])
    weeks = pd.date_range("2024-03-01", "2025-05-30", freq="W-FRI")
    result = run_backtest(price_db, _predictions(weeks), BacktestConfig(top_n=1, rebalance_weeks=4))
    strat = result.metrics["strategy"]
    ew = result.metrics["equal_weight_universe"]
    assert strat["total_return"] > ew["total_return"]
    assert strat["sharpe"] > ew["sharpe"]
    assert result.holdings_log[0][1] == ["WIN"]


def test_costs_reduce_returns(price_db, monkeypatch) -> None:
    import moi.backtest.engine as eng

    monkeypatch.setattr(eng, "candidate_tickers", lambda: ["WIN", "LOSE", "FLAT"])
    weeks = pd.date_range("2024-03-01", "2025-05-30", freq="W-FRI")
    free = run_backtest(price_db, _predictions(weeks), BacktestConfig(top_n=2, cost_bps_per_side=0))
    costly = run_backtest(
        price_db, _predictions(weeks), BacktestConfig(top_n=2, cost_bps_per_side=100)
    )
    assert free.metrics["strategy"]["total_return"] > costly.metrics["strategy"]["total_return"]


def test_performance_metrics_basics() -> None:
    steady = pd.Series([0.01] * 52)
    m = performance_metrics(steady)
    assert m["ann_return"] > 0.5  # 1% weekly compounds to ~67%/yr
    assert m["max_drawdown"] == 0.0
    assert performance_metrics(pd.Series(dtype=float)) == {}
