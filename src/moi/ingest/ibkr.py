"""Interactive Brokers connectivity via ib_async.

Phase 0 scope: connect to a running TWS/IB Gateway (paper), read account summary and
positions, and fetch historical daily bars. No order placement lives here — execution
is a separate, deliberately isolated module (Phase 4).

ib_async runs its own asyncio event loop. We expose small synchronous helpers so the
rest of the app (CLI, collectors) does not need to be async-aware yet.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from moi.config import IBKRSettings, get_settings
from moi.logging import get_logger

if TYPE_CHECKING:
    from ib_async import IB

log = get_logger(__name__)


@dataclass
class AccountInfo:
    account: str
    net_liquidation: float | None
    total_cash: float | None
    positions: list[tuple[str, float, float]]  # (symbol, position, avg_cost)


@contextmanager
def ib_connection(settings: IBKRSettings | None = None) -> Iterator[IB]:
    """Yield a connected ib_async ``IB`` instance, disconnecting on exit.

    Raises a clear error if IB Gateway/TWS is not reachable so the CLI can print a hint.
    """
    from ib_async import IB  # imported lazily so unit tests don't require the package

    cfg = settings or get_settings().ibkr
    ib = IB()
    log.info("ibkr_connecting", host=cfg.host, port=cfg.port, client_id=cfg.client_id)
    try:
        ib.connect(cfg.host, cfg.port, clientId=cfg.client_id, readonly=cfg.readonly, timeout=10)
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        raise ConnectionError(
            f"Could not reach IB Gateway/TWS at {cfg.host}:{cfg.port}. "
            "Is it running with the API enabled? See docs/SETUP.md."
        ) from exc
    try:
        yield ib
    finally:
        if ib.isConnected():
            ib.disconnect()
        log.info("ibkr_disconnected")


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ping(settings: IBKRSettings | None = None) -> AccountInfo:
    """Connect, read account summary + positions, disconnect. Proves connectivity."""
    cfg = settings or get_settings().ibkr
    with ib_connection(cfg) as ib:
        summary = {row.tag: row.value for row in ib.accountSummary()}
        account = cfg.account or (ib.managedAccounts()[0] if ib.managedAccounts() else "unknown")
        positions = [
            (p.contract.symbol, float(p.position), float(p.avgCost)) for p in ib.positions()
        ]
        info = AccountInfo(
            account=account,
            net_liquidation=_to_float(summary.get("NetLiquidation")),
            total_cash=_to_float(summary.get("TotalCashValue")),
            positions=positions,
        )
    log.info("ibkr_ping_ok", account=info.account, positions=len(info.positions))
    return info
