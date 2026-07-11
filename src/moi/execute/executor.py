"""Order executor — the ONLY code path that can place a trade.

Hard rails, enforced in code and tested in tests/test_executor.py:
    1. Only suggestions with status = 'APPROVED' are considered (queue.approved_unexecuted),
       and approvals expire after MAX_APPROVAL_AGE_DAYS.
    2. Ticker must be an active, non-benchmark universe member (whitelist).
    3. No shorting: sells are capped at the currently held quantity.
    4. Per-order and per-day dollar caps from settings (day = journaled non-error orders,
       including 'unknown' outcomes, plus the current batch).
    5. Kill switch blocks everything — a DB flag OR the data/KILL file sentinel
       (the file works even while another process holds the DB write lock).
    6. Only paper accounts (DU...) unless settings.allow_live is explicitly true —
       and live batches additionally require interactive confirmation (moi execute).

Sizing is computed from LIVE broker state at execution time (held quantity times latest
close over sleeve NLV), never from the weights stored on the suggestion — a stale or
fresh-build suggestion can therefore never double-buy an existing position.

Every order is journaled to ``orders`` before submission. A failure AFTER placement
marks the order 'unknown' (not 'error'): it keeps consuming daily-cap budget and keeps
the suggestion blocked until ``sync_fills`` resolves the true broker state.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import duckdb

from moi.config import DATA_DIR, get_settings
from moi.logging import get_logger
from moi.runlog import new_run_id

log = get_logger(__name__)

SLIPPAGE_GUARD = 0.01  # limit price: last close ±1%
MAX_PRICE_AGE_DAYS = 7  # refuse to size an order from a close older than this
MAX_APPROVAL_AGE_DAYS = 7  # refuse approvals older than this (stale thesis/weights)
KILL_FILE = DATA_DIR / "KILL"


class SafetyError(RuntimeError):
    """Raised when a planned order violates a hard rail. Never bypassed."""


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #
def kill_switch_on(con: duckdb.DuckDBPyConnection) -> bool:
    """Kill switch state: the file sentinel OR the DB flag — either one blocks."""
    if KILL_FILE.exists():
        return True
    row = con.execute("SELECT value FROM controls WHERE key = 'kill_switch'").fetchone()
    return row is not None and row[0] == "on"


def set_kill_file(on: bool) -> None:
    """Set/clear the filesystem kill sentinel — works even when the DB is locked."""
    if on:
        KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_FILE.touch()
    else:
        KILL_FILE.unlink(missing_ok=True)


def set_kill_switch(con: duckdb.DuckDBPyConnection, on: bool) -> None:
    set_kill_file(on)
    con.execute(
        "INSERT OR REPLACE INTO controls (key, value, updated_at) VALUES ('kill_switch', ?, ?)",
        ["on" if on else "off", datetime.now()],
    )
    log.warning("kill_switch_set", on=on)


# --------------------------------------------------------------------------- #
# Trading unlock (arming window)
# --------------------------------------------------------------------------- #
UNLOCK_FILE = DATA_DIR / "UNLOCK"


def unlock_trading(key: str) -> datetime:
    """Validate the unlock key and open a timed execution window.

    File-based so it works regardless of DB locks. Raises SafetyError on a bad key
    or when no key is configured.
    """
    import hmac

    s = get_settings()
    if not s.trading_unlock_key:
        raise SafetyError("MOI_TRADING_UNLOCK_KEY is not configured — cannot unlock")
    if not hmac.compare_digest(key.strip(), s.trading_unlock_key):
        log.warning("trading_unlock_rejected")
        raise SafetyError("invalid unlock key")
    until = datetime.now() + timedelta(minutes=s.trading_unlock_minutes)
    UNLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    UNLOCK_FILE.write_text(until.isoformat())
    log.warning("trading_unlocked", until=until.isoformat(timespec="seconds"))
    return until


def lock_trading() -> None:
    """Close the execution window immediately."""
    UNLOCK_FILE.unlink(missing_ok=True)
    log.warning("trading_locked")


def trading_unlocked_until() -> datetime | None:
    """Window expiry if trading is currently unlocked, else None."""
    if not UNLOCK_FILE.exists():
        return None
    try:
        until = datetime.fromisoformat(UNLOCK_FILE.read_text().strip())
    except ValueError:
        return None
    return until if until > datetime.now() else None


# --------------------------------------------------------------------------- #
# Planning (pure decision logic — fully unit-tested, no broker needed)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlannedOrder:
    suggestion_id: str
    ticker: str
    side: str  # BUY | SELL
    quantity: int
    limit_price: float
    est_value: float


def plan_order(
    con: duckdb.DuckDBPyConnection,
    *,
    suggestion_id: str,
    ticker: str,
    action: str,
    target_weight: float,
    net_liquidation: float,
    held_quantity: float,
) -> PlannedOrder:
    """Validate a single approved suggestion against every rail and size the order.

    ``net_liquidation`` is the SLEEVE net-liq (account NLV minus benchmark-ETF value);
    the current weight is derived live from ``held_quantity`` — stored suggestion
    weights are display-only and never size an order.
    """
    settings = get_settings()

    if kill_switch_on(con):
        raise SafetyError("kill switch is ON — all trading blocked")

    row = con.execute(
        "SELECT status, decided_at FROM suggestions WHERE id = ?", [suggestion_id]
    ).fetchone()
    if row is None or row[0] != "APPROVED":
        raise SafetyError(f"suggestion {suggestion_id} is not APPROVED")
    decided_at = row[1]
    if decided_at is not None and datetime.now() - decided_at > timedelta(
        days=MAX_APPROVAL_AGE_DAYS
    ):
        raise SafetyError(
            f"approval is {(datetime.now() - decided_at).days} days old "
            f"(max {MAX_APPROVAL_AGE_DAYS}) — re-approve a fresh suggestion"
        )

    whitelisted = con.execute(
        "SELECT 1 FROM universe WHERE ticker = ? AND active AND NOT is_benchmark",
        [ticker],
    ).fetchone()
    if not whitelisted:
        raise SafetyError(f"{ticker} is not in the tradable universe whitelist")

    price_row = con.execute(
        "SELECT close, date FROM prices_daily WHERE ticker = ? ORDER BY date DESC LIMIT 1",
        [ticker],
    ).fetchone()
    if not price_row or not price_row[0]:
        raise SafetyError(f"no price on file for {ticker}")
    price = float(price_row[0])
    price_date: date = price_row[1]
    if (date.today() - price_date).days > MAX_PRICE_AGE_DAYS:
        raise SafetyError(
            f"{ticker}: latest stored price is from {price_date} "
            f"(> {MAX_PRICE_AGE_DAYS} days old) — refresh prices before trading"
        )

    if net_liquidation <= 0:
        raise SafetyError("sleeve net liquidation is not positive — refusing to size orders")

    if action in ("BUY", "ADD"):
        side = "BUY"
    elif action in ("SELL", "TRIM"):
        side = "SELL"
    else:
        raise SafetyError(f"{ticker}: unknown action {action!r}")

    current_weight = max(held_quantity, 0.0) * price / net_liquidation
    delta_value = (target_weight - current_weight) * net_liquidation
    if side == "BUY" and delta_value <= 0:
        raise SafetyError(
            f"{ticker}: already at/above target ({current_weight:.1%} vs "
            f"{target_weight:.1%}) — nothing to buy"
        )
    if side == "SELL" and delta_value >= 0:
        raise SafetyError(
            f"{ticker}: at/below target ({current_weight:.1%} vs "
            f"{target_weight:.1%}) — nothing to sell"
        )

    quantity = math.floor(abs(delta_value) / price)
    if quantity < 1:
        raise SafetyError(f"{ticker}: computed quantity is zero (delta ${delta_value:.0f})")

    if side == "SELL":
        if held_quantity <= 0:
            raise SafetyError(f"{ticker}: nothing held — shorting is not allowed")
        quantity = min(quantity, math.floor(held_quantity))  # never below zero position

    limit = price * (1 + SLIPPAGE_GUARD) if side == "BUY" else price * (1 - SLIPPAGE_GUARD)
    est_value = quantity * price
    if est_value > settings.max_order_usd:
        raise SafetyError(
            f"{ticker}: order ${est_value:,.0f} exceeds max_order_usd "
            f"${settings.max_order_usd:,.0f}"
        )

    if journaled_today(con) + est_value > settings.max_daily_usd:
        raise SafetyError(
            f"daily total ${journaled_today(con) + est_value:,.0f} would exceed "
            f"max_daily_usd ${settings.max_daily_usd:,.0f}"
        )

    return PlannedOrder(
        suggestion_id=suggestion_id,
        ticker=ticker,
        side=side,
        quantity=quantity,
        limit_price=round(limit, 2),
        est_value=est_value,
    )


def journaled_today(con: duckdb.DuckDBPyConnection) -> float:
    """Dollar total of today's journaled orders (everything except confirmed errors)."""
    from moi.db import scalar

    return float(
        scalar(
            con,
            """SELECT coalesce(sum(est_value), 0) FROM orders
               WHERE created_at >= current_date AND status != 'error'""",
        )
    )


def journal_order(
    con: duckdb.DuckDBPyConnection, plan: PlannedOrder, account: str, status: str, detail: str = ""
) -> str:
    order_id = new_run_id()
    con.execute(
        """INSERT INTO orders (order_id, suggestion_id, created_at, account, ticker, side,
                               quantity, limit_price, est_value, status, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            order_id,
            plan.suggestion_id,
            datetime.now(),
            account,
            plan.ticker,
            plan.side,
            plan.quantity,
            plan.limit_price,
            plan.est_value,
            status,
            detail,
        ],
    )
    return order_id


def sync_fills(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Reconcile journaled orders with IBKR state.

    Looks at open orders, this session's trades, AND completed orders (so GTC fills
    from earlier sessions are found). Matches on ``perm_id``, falling back to
    ``ib_order_id`` for orders journaled before their permId was known. Read-only
    against the broker — safe to run any time a gateway is up.
    """
    from moi.ingest.ibkr import ib_connection

    open_rows = con.execute(
        """SELECT order_id, perm_id, ib_order_id, ticker, created_at, suggestion_id
           FROM orders WHERE status IN ('submitted', 'unknown')"""
    ).fetchall()
    if not open_rows:
        return ["no submitted orders to sync"]

    results: list[str] = []
    with ib_connection() as ib:
        ib.reqAllOpenOrders()
        ib.sleep(1)
        trades = list(ib.trades())
        try:
            trades += list(ib.reqCompletedOrders(apiOnly=False))
        except Exception as exc:  # older gateways may not support it — degrade
            log.warning("completed_orders_unavailable", error=str(exc)[:120])
        by_perm = {t.order.permId: t for t in trades if t.order.permId}
        by_order_id = {t.order.orderId: t for t in trades if t.order.orderId}

        for order_id, perm_id, ib_order_id, ticker, created_at, suggestion_id in open_rows:
            trade = by_perm.get(perm_id) if perm_id else None
            if trade is None and ib_order_id:
                trade = by_order_id.get(ib_order_id)
            if trade is None:
                age_h = (datetime.now() - created_at).total_seconds() / 3600
                stuck = " — STUCK, investigate in TWS" if age_h > 24 else ""
                results.append(
                    f"{ticker}: no broker state found (perm_id={perm_id}, age {age_h:.0f}h){stuck}"
                )
                continue
            if not perm_id and trade.order.permId:
                con.execute(
                    "UPDATE orders SET perm_id = ? WHERE order_id = ?",
                    [trade.order.permId, order_id],
                )
            st = trade.orderStatus
            if st.status == "Filled":
                con.execute(
                    """UPDATE orders SET status = 'filled', filled_at = ?, fill_price = ?,
                       detail = ? WHERE order_id = ?""",
                    [datetime.now(), st.avgFillPrice or None, st.status, order_id],
                )
                results.append(f"{ticker}: FILLED @ {st.avgFillPrice}")
            elif st.status in ("Cancelled", "ApiCancelled", "Inactive"):
                con.execute(
                    "UPDATE orders SET status = 'cancelled', detail = ? WHERE order_id = ?",
                    [st.status, order_id],
                )
                results.append(
                    f"{ticker}: {st.status} — suggestion {suggestion_id} was marked "
                    "EXECUTED but 0 shares traded; review it"
                )
            else:
                results.append(f"{ticker}: still {st.status}")
    return results


# --------------------------------------------------------------------------- #
# Execution (broker side)
# --------------------------------------------------------------------------- #
def execute_approved(
    con: duckdb.DuckDBPyConnection,
    confirm: Callable[[str, list[PlannedOrder]], bool] | None = None,
) -> list[str]:
    """Plan every approved-unexecuted suggestion, then submit. Returns summary lines.

    Two phases: all orders are planned first (so ``confirm`` — if given — sees the
    complete batch: account + orders + total dollars before anything is sent), then
    placed one by one with a kill-switch re-check before each submission.
    """
    from ib_async import LimitOrder, Stock

    from moi.execute.queue import approved_unexecuted
    from moi.ingest.ibkr import ib_connection

    settings = get_settings()
    work = approved_unexecuted(con)
    if not work:
        return ["nothing approved to execute"]
    if kill_switch_on(con):
        raise SafetyError("kill switch is ON — all trading blocked")

    results: list[str] = []
    with ib_connection() as ib:
        accounts = ib.managedAccounts()
        account = settings.ibkr.account or (accounts[0] if accounts else "")
        if not account.startswith("DU") and not settings.allow_live:
            raise SafetyError(
                f"account {account!r} is not a paper account (DU...) and allow_live is false"
            )
        if not account.startswith("DU") and settings.trading_unlock_key:
            until = trading_unlocked_until()
            if until is None:
                raise SafetyError(
                    "trading is LOCKED — run `moi unlock` (or use the dashboard sidebar) "
                    f"to open a {settings.trading_unlock_minutes}-minute execution window"
                )
            log.info("trading_window_open", until=until.isoformat(timespec="seconds"))
        summary = {r.tag: r.value for r in ib.accountSummary()}
        net_liq = float(summary.get("NetLiquidation", 0) or 0)
        held = {p.contract.symbol: float(p.position) for p in ib.positions()}
        sleeve_nlv = net_liq - _benchmark_value(con, held)

        # Phase 1: plan the whole batch (nothing journaled or sent yet).
        plans: list[PlannedOrder] = []
        batch_total = journaled_today(con)
        for suggestion_id, ticker, action, _cw, tw in work:
            try:
                plan = plan_order(
                    con,
                    suggestion_id=suggestion_id,
                    ticker=ticker,
                    action=action,
                    target_weight=tw or 0.0,
                    net_liquidation=sleeve_nlv,
                    held_quantity=held.get(ticker, 0.0),
                )
                if batch_total + plan.est_value > settings.max_daily_usd:
                    raise SafetyError(
                        f"batch total ${batch_total + plan.est_value:,.0f} would exceed "
                        f"max_daily_usd ${settings.max_daily_usd:,.0f}"
                    )
                batch_total += plan.est_value
                plans.append(plan)
            except SafetyError as exc:
                log.warning("order_refused", ticker=ticker, reason=str(exc))
                results.append(f"REFUSED {ticker}: {exc}")

        if not plans:
            return results or ["nothing to execute"]
        if confirm is not None and not confirm(account, plans):
            results.append("aborted before submission — no orders sent")
            return results

        # Phase 2: journal + place, re-checking the kill switch before every order.
        for plan in plans:
            if kill_switch_on(con):
                results.append(f"HALTED before {plan.ticker}: kill switch flipped ON")
                break
            order_id = journal_order(con, plan, account, "submitted")
            placed = False
            try:
                contract = Stock(plan.ticker, "SMART", "USD")
                order = LimitOrder(plan.side, plan.quantity, plan.limit_price, tif="GTC")
                # Conservative: once placeOrder is invoked the order may have reached
                # the broker even if the call itself raises mid-transmit.
                placed = True
                trade = ib.placeOrder(contract, order)
                for _ in range(10):  # wait for the broker to assign a durable permId
                    if trade.order.permId:
                        break
                    ib.sleep(0.5)
                con.execute(
                    "UPDATE orders SET ib_order_id = ?, perm_id = ?, detail = ? WHERE order_id = ?",
                    [
                        trade.order.orderId,
                        trade.order.permId or None,
                        trade.orderStatus.status,
                        order_id,
                    ],
                )
                con.execute(
                    "UPDATE suggestions SET status = 'EXECUTED' WHERE id = ?",
                    [plan.suggestion_id],
                )
                results.append(
                    f"SUBMITTED {plan.side} {plan.quantity} {plan.ticker} @ {plan.limit_price}"
                )
                log.info("order_submitted", ticker=plan.ticker, qty=plan.quantity)
            except Exception as exc:
                # After placeOrder the order may be live at the broker: 'unknown' keeps
                # it counted against the daily cap and blocks re-queue until sync_fills
                # resolves it. Only a pre-placement failure is a clean 'error'.
                status = "unknown" if placed else "error"
                con.execute(
                    "UPDATE orders SET status = ?, detail = ? WHERE order_id = ?",
                    [status, str(exc)[:300], order_id],
                )
                tag = "UNKNOWN (order may be live — run `moi orders --sync`)" if placed else "ERROR"
                results.append(f"{tag} {plan.ticker}: {exc}")
    return results


def _benchmark_value(con: duckdb.DuckDBPyConnection, held: dict[str, float]) -> float:
    """Market value of held benchmark ETFs (latest stored close, live quantity)."""
    benchmarks = {
        t for (t,) in con.execute("SELECT ticker FROM universe WHERE is_benchmark").fetchall()
    }
    total = 0.0
    for symbol, qty in held.items():
        if symbol in benchmarks and qty > 0:
            row = con.execute(
                "SELECT close FROM prices_daily WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                [symbol],
            ).fetchone()
            if row and row[0]:
                total += qty * float(row[0])
    return total
