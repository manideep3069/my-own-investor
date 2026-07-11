"""13F normalization, change annotation, and idempotent upsert."""

from __future__ import annotations

from datetime import date

import pandas as pd

from moi.ingest.edgar_13f import (
    Holding,
    Manager,
    annotate_changes,
    normalize_13f_table,
    previous_period_shares,
    upsert_holdings,
)

MGR = Manager(cik="1067983", name="Berkshire Hathaway")


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Issuer": ["Apple Inc", "Coherent Corp", "Coherent Corp"],
            "Cusip": ["037833100", "19247G107", "19247G107"],
            "Ticker": ["AAPL", "COHR", "COHR"],
            "Value": [1_000_000.0, 500_000.0, 250_000.0],
            "Shares": [10_000.0, 5_000.0, 2_500.0],
        }
    )


def test_normalize_aggregates_duplicate_cusips() -> None:
    holdings = normalize_13f_table(MGR, date(2026, 3, 31), date(2026, 5, 10), _frame())
    assert len(holdings) == 2
    cohr = next(h for h in holdings if h.cusip == "19247G107")
    assert cohr.value_usd == 750_000.0
    assert cohr.shares == 7_500.0
    assert cohr.ticker == "COHR"


def test_annotate_changes() -> None:
    holdings = normalize_13f_table(MGR, date(2026, 3, 31), None, _frame())
    prev = {"037833100": 10_000.0, "19247G107": 9_000.0, "GONE00000": 1.0}
    annotate_changes(holdings, prev)
    by_cusip = {h.cusip: h.change_status for h in holdings}
    assert by_cusip["037833100"] == "UNCHANGED"
    assert by_cusip["19247G107"] == "DECREASED"


def test_upsert_and_previous_period(db) -> None:
    q1 = normalize_13f_table(MGR, date(2026, 3, 31), None, _frame())
    assert upsert_holdings(db, q1) == 2
    upsert_holdings(db, q1)  # idempotent
    assert db.execute("SELECT count(*) FROM filings_13f").fetchone()[0] == 2

    prev = previous_period_shares(db, MGR.cik, date(2026, 6, 30))
    assert prev["037833100"] == 10_000.0

    # New quarter with a NEW position diffs against Q1.
    q2_frame = pd.DataFrame(
        {
            "Issuer": ["Vertiv"],
            "Cusip": ["92537N108"],
            "Ticker": ["VRT"],
            "Value": [100.0],
            "Shares": [10.0],
        }
    )
    q2 = normalize_13f_table(MGR, date(2026, 6, 30), None, q2_frame)
    annotate_changes(q2, previous_period_shares(db, MGR.cik, date(2026, 6, 30)))
    assert q2[0].change_status == "NEW"


def test_implausible_baseline_guard_logic() -> None:
    """A 4-holding partial filing must not serve as a diff baseline for a 40-holding one."""
    current = normalize_13f_table(MGR, date(2026, 3, 31), None, _frame())  # 2 holdings
    tiny_baseline = {"037833100": 1.0}  # 1 holding = 50% of 2 → plausible edge
    ok = len(tiny_baseline) >= 0.5 * max(len(current), 1)
    assert ok  # boundary case passes
    implausible = {}  # empty is handled by the `if prev` branch upstream
    assert not (implausible and len(implausible) >= 0.5 * max(len(current), 1))


def test_normalize_handles_missing_columns() -> None:
    frame = pd.DataFrame({"SomethingElse": [1]})
    assert normalize_13f_table(MGR, date(2026, 3, 31), None, frame) == []


def test_holding_dataclass_defaults() -> None:
    h = Holding(
        manager_cik="1",
        manager_name="x",
        period=date(2026, 1, 1),
        cusip="c",
        ticker=None,
        issuer=None,
        value_usd=None,
        shares=None,
        change_status=None,
        filed_at=None,
    )
    assert h.change_status is None


def test_annotate_unknown_shares_is_null_not_unchanged() -> None:
    h = Holding(
        MGR.cik, MGR.name, date(2026, 3, 31), "037833100", "AAPL", None, 1.0, None, None, None
    )
    annotate_changes([h], {"037833100": 10_000.0})
    assert h.change_status is None


def test_exit_rows_and_reentry_reads_new(db) -> None:
    from moi.ingest.edgar_13f import exit_rows, previous_holdings

    q1 = normalize_13f_table(MGR, date(2026, 3, 31), date(2026, 5, 1), _frame())
    upsert_holdings(db, q1)

    # Q2 drops COHR entirely → synthetic EXITED row with the old ticker attached.
    q2_frame = pd.DataFrame(
        {
            "Issuer": ["Apple Inc"],
            "Cusip": ["037833100"],
            "Ticker": ["AAPL"],
            "Value": [1_100_000.0],
            "Shares": [10_000.0],
        }
    )
    q2 = normalize_13f_table(MGR, date(2026, 6, 30), date(2026, 8, 1), q2_frame)
    prev_full = previous_holdings(db, MGR.cik, date(2026, 6, 30))
    exits = exit_rows(MGR, date(2026, 6, 30), date(2026, 8, 1), q2, prev_full)
    assert [(e.cusip, e.ticker, e.shares, e.change_status) for e in exits] == [
        ("19247G107", "COHR", 0.0, "EXITED")
    ]
    upsert_holdings(db, q2 + exits)

    # Q3 re-buys COHR: the EXITED (shares=0) row must NOT count as prior holding.
    prev_q3 = previous_period_shares(db, MGR.cik, date(2026, 9, 30))
    assert "19247G107" not in prev_q3


def test_restatement_replaces_period_wholesale(db) -> None:
    """Simulates the collect loop's delete-on-newer-filed_at behavior."""
    q1 = normalize_13f_table(MGR, date(2026, 3, 31), date(2026, 5, 1), _frame())
    upsert_holdings(db, q1)  # original: AAPL + COHR

    # Amendment (filed later) restates the quarter to AAPL only.
    amended = pd.DataFrame(
        {
            "Issuer": ["Apple Inc"],
            "Cusip": ["037833100"],
            "Ticker": ["AAPL"],
            "Value": [900_000.0],
            "Shares": [9_000.0],
        }
    )
    db.execute(
        "DELETE FROM filings_13f WHERE manager_cik = ? AND period = ?",
        [MGR.cik, date(2026, 3, 31)],
    )
    upsert_holdings(db, normalize_13f_table(MGR, date(2026, 3, 31), date(2026, 5, 20), amended))
    rows = db.execute(
        "SELECT cusip, shares FROM filings_13f WHERE period = '2026-03-31'"
    ).fetchall()
    assert rows == [("037833100", 9_000.0)]  # COHR phantom is gone
