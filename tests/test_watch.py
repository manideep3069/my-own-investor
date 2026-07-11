"""Urgent trigger checks on synthetic data."""

from __future__ import annotations

from datetime import date, timedelta

from moi.orchestrator.watch import big_move_alerts, whale_filing_alerts


def _seed_universe(db, ticker: str = "ALAB") -> None:
    db.execute(
        "INSERT OR REPLACE INTO universe (ticker, is_benchmark, active) VALUES (?, FALSE, TRUE)",
        [ticker],
    )


def test_big_move_fires(db) -> None:
    _seed_universe(db)
    d1, d2 = date.today() - timedelta(days=1), date.today()
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) VALUES ('ALAB', ?, 100, 't')",
        [d1],
    )
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) VALUES ('ALAB', ?, 85, 't')",
        [d2],
    )
    alerts = big_move_alerts(db)
    assert len(alerts) == 1
    assert "ALAB" in alerts[0].message
    assert "-15.0%" in alerts[0].message


def test_small_move_quiet(db) -> None:
    _seed_universe(db)
    d1, d2 = date.today() - timedelta(days=1), date.today()
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) VALUES ('ALAB', ?, 100, 't')",
        [d1],
    )
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) VALUES ('ALAB', ?, 103, 't')",
        [d2],
    )
    assert big_move_alerts(db) == []


def test_whale_filing_alert(db) -> None:
    _seed_universe(db, "COHR")
    db.execute(
        """INSERT INTO filings_13f
           (manager_cik, manager_name, period, cusip, ticker, change_status, filed_at)
           VALUES ('1', 'Berkshire', '2026-03-31', 'c1', 'COHR', 'NEW', current_date)"""
    )
    alerts = whale_filing_alerts(db)
    assert len(alerts) == 1
    assert "COHR" in alerts[0].message


def test_alerts_dedup_within_cooldown(db) -> None:
    """The same alert must not re-fire on consecutive runs."""
    from moi.orchestrator.watch import Alert, _fresh

    first = _fresh(db, [Alert("data_quality", "news_items is stale", key="news_items")])
    assert len(first) == 1
    again = _fresh(db, [Alert("data_quality", "news_items is stale", key="news_items")])
    assert again == []


def test_stuck_order_and_stale_approval_alerts(db) -> None:
    from datetime import datetime, timedelta

    from moi.orchestrator.watch import execution_alerts

    db.execute(
        """INSERT INTO orders (order_id, suggestion_id, created_at, ticker, side, quantity,
           est_value, status) VALUES ('o1', 's1', ?, 'ALAB', 'BUY', 1, 100, 'submitted')""",
        [datetime.now() - timedelta(hours=48)],
    )
    db.execute(
        """INSERT INTO suggestions (id, created_at, week_end, ticker, action, status, decided_at)
           VALUES ('s2', ?, '2026-07-01', 'CRDO', 'BUY', 'APPROVED', ?)""",
        [datetime.now() - timedelta(days=5), datetime.now() - timedelta(days=5)],
    )
    kinds = {a.kind for a in execution_alerts(db)}
    assert kinds == {"stuck_order", "stale_approval"}
