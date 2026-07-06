"""Assemble the modeling dataset: wide ticker features + market features + labels."""

from __future__ import annotations

import duckdb
import pandas as pd

from moi.features.market import MARKET_TICKER
from moi.features.panels import weekly_close_panel
from moi.ml.labels import HORIZON_WEEKS, forward_relative_returns
from moi.universe import candidate_tickers


def load_dataset(
    con: duckdb.DuckDBPyConnection, horizon_weeks: int = HORIZON_WEEKS
) -> tuple[pd.DataFrame, list[str]]:
    """Return (df, feature_cols).

    df columns: ticker, week_end, <features...>, label. Market features are broadcast
    to every ticker row of the same week. Rows without labels (most recent H weeks)
    are kept with label = NaN — they are the live scoring rows.
    """
    feats = con.execute(
        "SELECT ticker, week_end, feature, value FROM features_weekly"
    ).df()
    if feats.empty:
        raise RuntimeError("features_weekly is empty — run `moi features build` first.")
    feats["week_end"] = pd.to_datetime(feats["week_end"])

    ticker_rows = feats[feats["ticker"] != MARKET_TICKER]
    wide = ticker_rows.pivot_table(
        index=["ticker", "week_end"], columns="feature", values="value"
    ).reset_index()

    market_rows = feats[feats["ticker"] == MARKET_TICKER]
    if not market_rows.empty:
        mkt_wide = market_rows.pivot_table(
            index="week_end", columns="feature", values="value"
        ).reset_index()
        wide = wide.merge(mkt_wide, on="week_end", how="left")

    closes = weekly_close_panel(con)
    labels = forward_relative_returns(closes, candidate_tickers(), horizon_weeks)
    labels["week_end"] = pd.to_datetime(labels["week_end"])
    df = wide.merge(labels, on=["ticker", "week_end"], how="left")

    feature_cols = [c for c in df.columns if c not in ("ticker", "week_end", "label")]
    # Require the core momentum block to exist so early sparse weeks don't add noise.
    df = df.dropna(subset=["ret_13w"])
    return df.sort_values(["week_end", "ticker"]).reset_index(drop=True), feature_cols
