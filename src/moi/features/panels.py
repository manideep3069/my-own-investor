"""Weekly panels derived from daily prices.

All downstream features and labels are built from these panels. Weeks end on Friday
(``W-FRI``); a week's value uses only data up to and including that Friday.
"""

from __future__ import annotations

import duckdb
import pandas as pd


def weekly_close_panel(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Weekly adjusted-close panel: index = week_end (Fri), columns = tickers."""
    df = con.execute(
        "SELECT ticker, date, coalesce(adj_close, close) AS px FROM prices_daily"
    ).df()
    df["date"] = pd.to_datetime(df["date"])
    panel = df.pivot_table(index="date", columns="ticker", values="px")
    return panel.resample("W-FRI").last()


def weekly_dollar_volume_panel(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Weekly mean daily dollar volume: index = week_end, columns = tickers."""
    df = con.execute(
        "SELECT ticker, date, close * volume AS dv FROM prices_daily WHERE volume IS NOT NULL"
    ).df()
    df["date"] = pd.to_datetime(df["date"])
    panel = df.pivot_table(index="date", columns="ticker", values="dv")
    return panel.resample("W-FRI").mean()


def to_long(features: dict[str, pd.DataFrame], tickers: list[str]) -> pd.DataFrame:
    """Stack {feature_name: panel} into long rows (ticker, week_end, feature, value)."""
    frames = []
    for name, panel in features.items():
        cols = [t for t in tickers if t in panel.columns]
        long = panel[cols].stack().reset_index()
        long.columns = ["week_end", "ticker", "value"]
        long["feature"] = name
        frames.append(long)
    if not frames:
        return pd.DataFrame(columns=["ticker", "week_end", "feature", "value"])
    out = pd.concat(frames, ignore_index=True)
    return out[["ticker", "week_end", "feature", "value"]].dropna(subset=["value"])
