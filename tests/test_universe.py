"""Universe loading and sync."""

from __future__ import annotations

from moi.universe import all_tickers, candidate_tickers, load_universe, sync_universe


def test_load_universe_has_candidates_and_benchmarks() -> None:
    insts = load_universe()
    tickers = {i.ticker for i in insts}
    assert "ALAB" in tickers
    assert "SPY" in tickers
    benches = {i.ticker for i in insts if i.is_benchmark}
    assert "SPY" in benches
    assert "ALAB" not in benches


def test_candidate_excludes_benchmarks() -> None:
    assert "SPY" not in candidate_tickers()
    assert "SPY" in all_tickers()


def test_sync_universe_idempotent(db) -> None:
    n1 = sync_universe(db)
    n2 = sync_universe(db)
    assert n1 == n2
    rows = db.execute("SELECT count(*) FROM universe").fetchone()[0]
    assert rows == n1
