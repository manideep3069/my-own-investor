"""Feature store orchestration: build all weekly features and upsert into DuckDB."""

from __future__ import annotations

import duckdb
import pandas as pd

from moi.features.market import market_features
from moi.features.momentum import momentum_features
from moi.features.panels import to_long, weekly_close_panel, weekly_dollar_volume_panel
from moi.features.whales import insider_features, whale_13f_features
from moi.logging import get_logger
from moi.runlog import track_run
from moi.universe import candidate_tickers

log = get_logger(__name__)


def upsert_features(con: duckdb.DuckDBPyConnection, frame: pd.DataFrame) -> int:
    """Idempotently upsert long-format feature rows."""
    if frame.empty:
        return 0
    rows = [
        (str(r.ticker), pd.Timestamp(r.week_end).date(), str(r.feature), float(r.value))
        for r in frame.itertuples(index=False)
    ]
    con.executemany(
        """
        INSERT INTO features_weekly (ticker, week_end, feature, value)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (ticker, week_end, feature) DO UPDATE SET value = excluded.value
        """,
        rows,
    )
    return len(rows)


def build_features(con: duckdb.DuckDBPyConnection) -> int:
    """Compute every feature family over the full price history and store it."""
    tickers = candidate_tickers()
    closes = weekly_close_panel(con)
    dollar_vol = weekly_dollar_volume_panel(con)
    week_ends = closes.index

    total = 0
    with track_run(con, job="features.build") as run:
        mom = to_long(momentum_features(closes, dollar_vol), tickers)
        total += upsert_features(con, mom)
        log.info("features_momentum", rows=len(mom))

        w13f = whale_13f_features(con, week_ends, tickers)
        total += upsert_features(con, w13f)
        log.info("features_13f", rows=len(w13f))

        ins = insider_features(con, week_ends, tickers)
        total += upsert_features(con, ins)
        log.info("features_insider", rows=len(ins))

        mkt = market_features(con, week_ends, closes)
        total += upsert_features(con, mkt)
        log.info("features_market", rows=len(mkt))

        run.add_rows(total)
    return total
