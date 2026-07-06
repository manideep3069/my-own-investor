"""Suggestion diffing and persistence."""

from __future__ import annotations

import pandas as pd

from moi.report.suggestions import Action, diff_actions, store_suggestions


def test_diff_actions_all_cases() -> None:
    target = {"A": 0.08, "B": 0.08, "C": 0.08}
    current = {"B": 0.02, "C": 0.085, "D": 0.05}
    actions = {a.ticker: a.action for a in diff_actions(target, current)}
    assert actions == {"A": "BUY", "B": "ADD", "D": "SELL"}  # C within band → no action


def test_diff_orders_buys_and_sells_first() -> None:
    target = {"A": 0.08, "B": 0.10}
    current = {"B": 0.02, "Z": 0.05}
    kinds = [a.action for a in diff_actions(target, current)]
    assert kinds == ["BUY", "SELL", "ADD"]


def test_diff_empty_current_is_fresh_build() -> None:
    target = {"A": 0.05, "B": 0.05}
    actions = diff_actions(target, {})
    assert all(a.action == "BUY" for a in actions)


def test_store_suggestions(db) -> None:
    acts = [Action("ALAB", "BUY", 0.0, 0.083, score=0.5, thesis="t", bear_case="b")]
    n = store_suggestions(db, pd.Timestamp("2026-07-10"), acts)
    assert n == 1
    row = db.execute("SELECT ticker, action, status, thesis FROM suggestions").fetchone()
    assert row == ("ALAB", "BUY", "PENDING", "t")


def test_new_run_supersedes_old_pending(db) -> None:
    week1 = [Action("ALAB", "BUY", 0.0, 0.08), Action("COHR", "BUY", 0.0, 0.08)]
    store_suggestions(db, pd.Timestamp("2026-07-10"), week1)
    # One gets approved before the next run; approved rows must NOT be touched.
    sid = db.execute("SELECT id FROM suggestions WHERE ticker='ALAB'").fetchone()[0]
    db.execute("UPDATE suggestions SET status='APPROVED' WHERE id=?", [sid])

    week2 = [Action("COHR", "BUY", 0.0, 0.09)]
    store_suggestions(db, pd.Timestamp("2026-07-17"), week2)

    counts = dict(db.execute("SELECT status, count(*) FROM suggestions GROUP BY 1").fetchall())
    assert counts == {"APPROVED": 1, "SUPERSEDED": 1, "PENDING": 1}
    pending = db.execute(
        "SELECT ticker, week_end FROM suggestions WHERE status='PENDING'"
    ).fetchone()
    assert str(pending[0]) == "COHR" and str(pending[1]) == "2026-07-17"
