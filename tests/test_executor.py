"""Executor safety rails — each one must refuse, loudly."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import moi.ingest.ibkr as ibkr_mod
from moi.execute.executor import (
    SafetyError,
    execute_approved,
    journaled_today,
    kill_switch_on,
    plan_order,
    set_kill_file,
    set_kill_switch,
)
from moi.execute.queue import decide

NET_LIQ = 100_000.0


@pytest.fixture(autouse=True)
def _no_kill_file():
    set_kill_file(False)
    yield
    set_kill_file(False)


def _seed(db, *, ticker="ALAB", status="APPROVED", benchmark=False, price=100.0) -> str:
    db.execute(
        "INSERT OR REPLACE INTO universe (ticker, is_benchmark, active) VALUES (?, ?, TRUE)",
        [ticker, benchmark],
    )
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) VALUES (?, current_date, ?, 't')",
        [ticker, price],
    )
    sid = f"sug-{ticker}"
    db.execute(
        """INSERT INTO suggestions (id, created_at, week_end, ticker, action,
           current_weight, target_weight, status)
           VALUES (?, ?, '2026-07-10', ?, 'BUY', 0.0, 0.05, ?)""",
        [sid, datetime.now(), ticker, status],
    )
    return sid


def _plan(db, sid, **overrides):
    kwargs = dict(
        suggestion_id=sid,
        ticker="ALAB",
        action="BUY",
        target_weight=0.05,
        net_liquidation=NET_LIQ,
        held_quantity=0.0,
    )
    kwargs.update(overrides)
    return plan_order(db, **kwargs)


def test_happy_path_sizes_correctly(db) -> None:
    sid = _seed(db)
    plan = _plan(db, sid)
    assert plan.side == "BUY"
    assert plan.quantity == 50  # 5% of 100k = $5,000 at $100
    assert plan.limit_price == pytest.approx(101.0)  # +1% slippage guard


def test_buy_sized_from_live_holdings_not_stored_weight(db) -> None:
    """A fresh-build BUY (stored current 0%) must shrink to the live delta."""
    sid = _seed(db)
    # Already hold 30 shares = $3,000 = 3%; target 5% → only $2,000 more.
    plan = _plan(db, sid, held_quantity=30.0)
    assert plan.quantity == 20


def test_refuses_buy_already_at_target(db) -> None:
    sid = _seed(db)
    with pytest.raises(SafetyError, match="already at/above target"):
        _plan(db, sid, held_quantity=60.0)  # 6% held vs 5% target


def test_refuses_unapproved(db) -> None:
    sid = _seed(db, status="PENDING")
    with pytest.raises(SafetyError, match="not APPROVED"):
        _plan(db, sid)


def test_refuses_stale_approval(db) -> None:
    sid = _seed(db)
    db.execute(
        "UPDATE suggestions SET decided_at = ? WHERE id = ?",
        [datetime.now() - timedelta(days=10), sid],
    )
    with pytest.raises(SafetyError, match="days old"):
        _plan(db, sid)


def test_refuses_stale_price(db) -> None:
    sid = _seed(db, ticker="OLDP")
    db.execute("DELETE FROM prices_daily WHERE ticker = 'OLDP'")
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) "
        "VALUES ('OLDP', current_date - INTERVAL 30 DAY, 100.0, 't')"
    )
    with pytest.raises(SafetyError, match="days old"):
        _plan(db, sid, ticker="OLDP")


def test_refuses_non_whitelisted_ticker(db) -> None:
    sid = _seed(db)  # approves ALAB
    with pytest.raises(SafetyError, match="whitelist"):
        _plan(db, sid, ticker="GME")  # not in universe at all


def test_refuses_benchmark_ticker(db) -> None:
    sid = _seed(db, ticker="SPY", benchmark=True)
    with pytest.raises(SafetyError, match="whitelist"):
        _plan(db, sid, ticker="SPY")


def test_refuses_oversized_order(db) -> None:
    sid = _seed(db)
    with pytest.raises(SafetyError, match="max_order_usd"):
        _plan(db, sid, target_weight=0.20)  # $20k > $8k default cap


def test_refuses_when_kill_switch_on(db) -> None:
    sid = _seed(db)
    set_kill_switch(db, True)
    assert kill_switch_on(db)
    with pytest.raises(SafetyError, match="kill switch"):
        _plan(db, sid)
    set_kill_switch(db, False)
    assert _plan(db, sid).quantity > 0  # recovers when switched off


def test_kill_file_sentinel_blocks_without_db_flag(db) -> None:
    sid = _seed(db)
    set_kill_file(True)  # file only — no controls row
    assert kill_switch_on(db)
    with pytest.raises(SafetyError, match="kill switch"):
        _plan(db, sid)
    set_kill_file(False)
    assert not kill_switch_on(db)


def test_refuses_short_sell(db) -> None:
    sid = _seed(db)
    db.execute("UPDATE suggestions SET action = 'SELL' WHERE id = ?", [sid])
    with pytest.raises(SafetyError, match=r"nothing to sell|short"):
        _plan(db, sid, action="SELL", target_weight=0.0, held_quantity=0.0)


def test_sell_capped_at_held_quantity(db) -> None:
    sid = _seed(db)
    db.execute("UPDATE suggestions SET action = 'SELL' WHERE id = ?", [sid])
    plan = _plan(db, sid, action="SELL", target_weight=0.0, held_quantity=30.7)
    assert plan.side == "SELL"
    assert plan.quantity == 30  # never sells more than (whole) held shares


def test_refuses_unknown_action(db) -> None:
    sid = _seed(db)
    with pytest.raises(SafetyError, match="unknown action"):
        _plan(db, sid, action="HEDGE")


def test_refuses_daily_cap_and_counts_unknown_orders(db) -> None:
    sid = _seed(db)
    # An 'unknown' outcome (possibly live at the broker) must consume budget.
    db.execute(
        """INSERT INTO orders (order_id, suggestion_id, created_at, ticker, side, quantity,
           est_value, status) VALUES ('o1', 's1', ?, 'X', 'BUY', 1, 29000, 'unknown')""",
        [datetime.now()],
    )
    assert journaled_today(db) == pytest.approx(29000)
    with pytest.raises(SafetyError, match="max_daily_usd"):
        _plan(db, sid)  # $5k more would exceed the $30k daily cap
    # A confirmed error frees the budget.
    db.execute("UPDATE orders SET status = 'error' WHERE order_id = 'o1'")
    assert _plan(db, sid).quantity == 50


def test_decide_transitions(db) -> None:
    sid = _seed(db, status="PENDING")
    assert decide(db, sid, "approved")
    assert (
        db.execute("SELECT status FROM suggestions WHERE id=?", [sid]).fetchone()[0] == "APPROVED"
    )
    assert not decide(db, sid, "SNOOZED")  # approved → snoozed is not a thing
    assert not decide(db, "missing", "APPROVED")
    with pytest.raises(ValueError, match="Invalid decision"):
        decide(db, sid, "EXECUTE")


def test_decide_revoke_approval_before_any_order(db) -> None:
    sid = _seed(db, status="PENDING")
    assert decide(db, sid, "APPROVED")
    assert decide(db, sid, "REJECTED")  # revocable while nothing was sent
    assert (
        db.execute("SELECT status FROM suggestions WHERE id=?", [sid]).fetchone()[0] == "REJECTED"
    )


def test_decide_revoke_blocked_once_order_exists(db) -> None:
    sid = _seed(db, status="PENDING")
    assert decide(db, sid, "APPROVED")
    db.execute(
        """INSERT INTO orders (order_id, suggestion_id, created_at, ticker, side, quantity,
           est_value, status) VALUES ('o2', ?, ?, 'ALAB', 'BUY', 1, 100, 'submitted')""",
        [sid, datetime.now()],
    )
    assert not decide(db, sid, "REJECTED")


# --------------------------------------------------------------------------- #
# execute_approved end-to-end (broker mocked)
# --------------------------------------------------------------------------- #
class _FakeIB:
    def __init__(self, *, explode_after_place: bool = False) -> None:
        self.explode_after_place = explode_after_place
        self.placed: list = []

    def managedAccounts(self):
        return ["DU111111"]

    def accountSummary(self):
        return [SimpleNamespace(tag="NetLiquidation", value=str(NET_LIQ))]

    def positions(self):
        return []

    def sleep(self, _s):
        return None

    def placeOrder(self, contract, order):
        self.placed.append((contract.symbol, order.action, order.totalQuantity))
        if self.explode_after_place:
            raise RuntimeError("socket dropped after transmit")
        return SimpleNamespace(
            order=SimpleNamespace(orderId=7, permId=4242),
            orderStatus=SimpleNamespace(status="Submitted"),
        )


@pytest.fixture()
def fake_ib(monkeypatch):
    holder: dict = {"ib": _FakeIB()}

    @contextmanager
    def fake_connection(settings=None):
        yield holder["ib"]

    monkeypatch.setattr(ibkr_mod, "ib_connection", fake_connection)
    return holder


def test_execute_approved_submits_and_journals(db, fake_ib) -> None:
    sid = _seed(db, status="PENDING")
    assert decide(db, sid, "APPROVED")
    results = execute_approved(db)
    assert any(r.startswith("SUBMITTED BUY 50 ALAB") for r in results)
    status, perm = db.execute(
        "SELECT status, perm_id FROM orders WHERE suggestion_id = ?", [sid]
    ).fetchone()
    assert (status, perm) == ("submitted", 4242)
    assert (
        db.execute("SELECT status FROM suggestions WHERE id=?", [sid]).fetchone()[0] == "EXECUTED"
    )
    # Re-run: nothing left to do — no duplicate submission.
    assert execute_approved(db) == ["nothing approved to execute"]


def test_execute_approved_confirm_abort_sends_nothing(db, fake_ib) -> None:
    sid = _seed(db, status="PENDING")
    assert decide(db, sid, "APPROVED")
    results = execute_approved(db, confirm=lambda account, plans: False)
    assert any("aborted before submission" in r for r in results)
    assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    assert fake_ib["ib"].placed == []


def test_execute_approved_post_placement_failure_is_unknown(db, fake_ib) -> None:
    """A crash after placeOrder must NOT free the cap or re-queue the suggestion."""
    fake_ib["ib"] = _FakeIB(explode_after_place=True)
    sid = _seed(db, status="PENDING")
    assert decide(db, sid, "APPROVED")
    results = execute_approved(db)
    assert any(r.startswith("UNKNOWN") for r in results)
    (status,) = db.execute("SELECT status FROM orders WHERE suggestion_id = ?", [sid]).fetchone()
    assert status == "unknown"
    assert journaled_today(db) > 0  # still consumes daily budget
    # The suggestion stays APPROVED but is blocked from the work list.
    assert execute_approved(db) == ["nothing approved to execute"]


# --------------------------------------------------------------------------- #
# Trading unlock window (arming rail)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def unlock_key(monkeypatch):
    from moi.config import get_settings
    from moi.execute.executor import lock_trading

    monkeypatch.setattr(get_settings(), "trading_unlock_key", "open-sesame")
    lock_trading()
    yield "open-sesame"
    lock_trading()


def test_unlock_rejects_bad_key(unlock_key) -> None:
    from moi.execute.executor import SafetyError, trading_unlocked_until, unlock_trading

    with pytest.raises(SafetyError, match="invalid unlock key"):
        unlock_trading("wrong")
    assert trading_unlocked_until() is None


def test_unlock_opens_and_lock_closes_window(unlock_key) -> None:
    from moi.execute.executor import lock_trading, trading_unlocked_until, unlock_trading

    until = unlock_trading("open-sesame")
    assert trading_unlocked_until() == until
    lock_trading()
    assert trading_unlocked_until() is None


def test_expired_window_reads_locked(unlock_key) -> None:
    from moi.execute.executor import UNLOCK_FILE, trading_unlocked_until

    UNLOCK_FILE.write_text((datetime.now() - timedelta(minutes=1)).isoformat())
    assert trading_unlocked_until() is None


def test_execute_refuses_live_account_while_locked(db, fake_ib, monkeypatch, unlock_key) -> None:
    from moi.config import get_settings
    from moi.execute.executor import SafetyError, unlock_trading

    monkeypatch.setattr(get_settings(), "allow_live", True)
    fake_ib["ib"].managedAccounts = lambda: ["U9999999"]  # live account
    sid = _seed(db, status="PENDING")
    assert decide(db, sid, "APPROVED")
    with pytest.raises(SafetyError, match="LOCKED"):
        execute_approved(db)
    # Unlock → same batch goes through.
    unlock_trading("open-sesame")
    results = execute_approved(db)
    assert any(r.startswith("SUBMITTED") for r in results)
