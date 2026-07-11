"""Daily OHLCV collector.

Sources: yfinance (free, reliable for backfill) and IBKR historical bars (when a gateway
is connected). Both normalize to the same :class:`PriceRow` shape and upsert idempotently
into ``prices_daily`` keyed by ``(ticker, date)`` — re-running never duplicates rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import duckdb

from moi.logging import get_logger
from moi.runlog import track_run
from moi.universe import all_tickers

log = get_logger(__name__)


@dataclass(frozen=True)
class PriceRow:
    ticker: str
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None
    volume: int | None
    source: str


# --------------------------------------------------------------------------- #
# Normalization (pure, unit-testable without network)
# --------------------------------------------------------------------------- #
def normalize_yf_frame(ticker: str, frame: object) -> list[PriceRow]:
    """Convert a single-ticker yfinance/pandas DataFrame into PriceRows.

    Expects columns Open/High/Low/Close and optionally Adj Close/Volume, indexed by date.
    """
    import math

    import pandas as pd  # local import keeps module import light

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []

    def cell(row: pd.Series, *names: str) -> float | None:
        for n in names:
            if n in row and not (isinstance(row[n], float) and math.isnan(row[n])):
                return float(row[n])
        return None

    rows: list[PriceRow] = []
    for idx, r in frame.iterrows():
        d = idx.date() if hasattr(idx, "date") else pd.Timestamp(idx).date()
        close = cell(r, "Close")
        vol = cell(r, "Volume")
        rows.append(
            PriceRow(
                ticker=ticker,
                date=d,
                open=cell(r, "Open"),
                high=cell(r, "High"),
                low=cell(r, "Low"),
                close=close,
                adj_close=cell(r, "Adj Close") or close,
                volume=int(vol) if vol is not None else None,
                source="yfinance",
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_yfinance(tickers: list[str], start: date, end: date) -> tuple[list[PriceRow], list[str]]:
    """Fetch daily bars via yfinance. Returns (rows, failed_tickers)."""
    import yfinance as yf

    rows: list[PriceRow] = []
    failed: list[str] = []
    # per-ticker loop: robust column handling and isolates a bad symbol
    for ticker in tickers:
        try:
            frame = yf.download(
                ticker,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            # yf may return MultiIndex columns for a single ticker; flatten them.
            if hasattr(frame, "columns") and getattr(frame.columns, "nlevels", 1) > 1:
                frame = frame.droplevel(1, axis=1)
            fetched = normalize_yf_frame(ticker, frame)
            if not fetched:
                failed.append(ticker)
            rows.extend(fetched)
            log.info("yf_fetched", ticker=ticker, rows=len(fetched))
        except Exception as exc:
            failed.append(ticker)
            log.warning("yf_fetch_failed", ticker=ticker, error=str(exc))
    return rows, failed


def fetch_ibkr(ib: object, tickers: list[str], years: int) -> tuple[list[PriceRow], list[str]]:
    """Fetch daily bars from IBKR historical data. Returns (rows, failed_tickers)."""
    from ib_async import Stock

    rows: list[PriceRow] = []
    failed: list[str] = []
    for ticker in tickers:
        try:
            contract = Stock(ticker, "SMART", "USD")
            bars = ib.reqHistoricalData(  # type: ignore[attr-defined]
                contract,
                endDateTime="",
                durationStr=f"{years} Y",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            for b in bars:
                d = (
                    b.date
                    if isinstance(b.date, date)
                    else datetime.strptime(str(b.date), "%Y%m%d").date()
                )
                rows.append(
                    PriceRow(
                        ticker=ticker,
                        date=d,
                        open=float(b.open),
                        high=float(b.high),
                        low=float(b.low),
                        close=float(b.close),
                        # TRADES bars are split- but not dividend-adjusted: storing them
                        # as adj_close would mix adjustment bases with yfinance rows.
                        adj_close=None,
                        volume=int(b.volume) if b.volume and b.volume > 0 else None,
                        source="ibkr",
                    )
                )
            log.info("ibkr_fetched", ticker=ticker)
        except Exception as exc:
            failed.append(ticker)
            log.warning("ibkr_fetch_failed", ticker=ticker, error=str(exc))
    return rows, failed


# --------------------------------------------------------------------------- #
# Persistence (idempotent)
# --------------------------------------------------------------------------- #
def upsert_prices(con: duckdb.DuckDBPyConnection, rows: list[PriceRow]) -> int:
    """Idempotently upsert price rows by (ticker, date). Returns rows processed."""
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO prices_daily
            (ticker, date, open, high, low, close, adj_close, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ticker, date) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            adj_close = excluded.adj_close,
            volume = excluded.volume,
            source = excluded.source
        """,
        [
            (r.ticker, r.date, r.open, r.high, r.low, r.close, r.adj_close, r.volume, r.source)
            for r in rows
        ],
    )
    return len(rows)


def tracked_tickers(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Universe + benchmarks + anything actually held in the account.

    Held non-universe tickers (from portfolio snapshots) get price history too so the
    dashboard can chart the real portfolio's performance.
    """
    held = [r[0] for r in con.execute("SELECT DISTINCT ticker FROM portfolio_snapshots").fetchall()]
    return sorted(set(all_tickers()) | set(held))


DIVERGENCE_TOLERANCE = 0.005  # overlap close mismatch beyond 0.5% → retro-adjustment


def full_window_start(years: int, today: date | None = None) -> date:
    today = today or date.today()
    return today - timedelta(days=int(years * 365.25) + 5)


def incremental_starts(
    con: duckdb.DuckDBPyConnection, years: int, tickers: list[str], today: date | None = None
) -> dict[str, date]:
    """Per-ticker refresh start: 7 days before THAT ticker's latest stored bar.

    Per-ticker (not a global max) so a symbol that failed for a while, or one newly
    added to the universe, is healed automatically instead of gaining a permanent gap.
    """
    full_start = full_window_start(years, today)
    latest: dict[str, date] = dict(
        con.execute("SELECT ticker, max(date) FROM prices_daily GROUP BY ticker").fetchall()
    )
    return {
        t: max(full_start, latest[t] - timedelta(days=7)) if t in latest else full_start
        for t in tickers
    }


def diverged_tickers(con: duckdb.DuckDBPyConnection, rows: list[PriceRow]) -> set[str]:
    """Tickers whose refetched bars disagree with stored closes on overlapping dates.

    yfinance retro-adjusts the whole history on splits (and Adj Close on dividends);
    a mismatch inside the overlap window means everything older is stale too, so the
    caller should full-refetch these tickers.
    """
    if not rows:
        return set()
    tickers = sorted({r.ticker for r in rows})
    min_date = min(r.date for r in rows)
    placeholders = ", ".join("?" for _ in tickers)
    stored = {
        (t, d): float(c)
        for t, d, c in con.execute(
            f"SELECT ticker, date, close FROM prices_daily "
            f"WHERE date >= ? AND close IS NOT NULL AND ticker IN ({placeholders})",
            [min_date, *tickers],
        ).fetchall()
    }
    out: set[str] = set()
    for r in rows:
        old = stored.get((r.ticker, r.date))
        if old and r.close and abs(r.close / old - 1) > DIVERGENCE_TOLERANCE:
            out.add(r.ticker)
    return out


def collect_prices(
    con: duckdb.DuckDBPyConnection,
    *,
    years: int,
    tickers: list[str] | None = None,
    source: str = "yfinance",
    full: bool = False,
) -> int:
    """Fetch and upsert daily prices for the universe. Returns NEW rows stored.

    Incremental by default with a per-ticker 7-day overlap; when the overlap disagrees
    with stored closes (split/dividend restatement) the ticker's whole history is
    refetched and replaced. ``full=True`` refetches everything.

    Args:
        source: "yfinance" (default, no gateway needed) or "ibkr" (requires connection).
    """
    from moi.db import scalar

    syms = tickers if tickers is not None else tracked_tickers(con)
    end = date.today()
    full_start = full_window_start(years, end)

    with track_run(con, job="collect.prices") as run:
        if source == "ibkr":
            from moi.ingest.ibkr import ib_connection

            with ib_connection() as ib:
                rows, failed = fetch_ibkr(ib, syms, years)
        elif full:
            rows, failed = fetch_yfinance(syms, full_start, end)
        else:
            starts = incremental_starts(con, years, syms, end)
            buckets: dict[date, list[str]] = {}
            for t, s in starts.items():
                buckets.setdefault(s, []).append(t)
            rows, failed = [], []
            for start_d, group in sorted(buckets.items()):
                r, f = fetch_yfinance(group, start_d, end)
                rows += r
                failed += f
            # Heal retroactive adjustments: replace the full history of any ticker
            # whose overlap window no longer matches what we stored.
            diverged = diverged_tickers(con, rows)
            if diverged:
                log.warning("price_series_diverged", tickers=sorted(diverged))
                healed, f2 = fetch_yfinance(sorted(diverged), full_start, end)
                ok_healed = {r.ticker for r in healed}
                if ok_healed:
                    ph = ", ".join("?" for _ in ok_healed)
                    con.execute(
                        f"DELETE FROM prices_daily WHERE ticker IN ({ph})", sorted(ok_healed)
                    )
                rows = [r for r in rows if r.ticker not in ok_healed] + healed
                failed += f2

        before = int(scalar(con, "SELECT count(*) FROM prices_daily"))
        processed = upsert_prices(con, rows)
        new_rows = int(scalar(con, "SELECT count(*) FROM prices_daily")) - before
        run.add_rows(new_rows)
        run.add_failures(len(set(failed)))
        run.detail = f"source={source} tickers={len(syms)} processed={processed}"
        if failed:
            run.detail += f" failed={','.join(sorted(set(failed))[:8])}"
    log.info("collect_prices_done", new=new_rows, processed=processed, source=source)
    return new_rows
