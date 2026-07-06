"""Whale features from 13F filings and insider Form 4 transactions.

Point-in-time rule: a filing exists for week ``w`` only if ``filed_at <= w`` — the
market couldn't see it before it was filed, so neither can the model.
"""

from __future__ import annotations

import duckdb
import pandas as pd

_ACTION_SCORE = {"NEW": 1.0, "INCREASED": 1.0, "DECREASED": -1.0, "UNCHANGED": 0.0}


def whale_13f_features(
    con: duckdb.DuckDBPyConnection, week_ends: pd.DatetimeIndex, tickers: list[str]
) -> pd.DataFrame:
    """Long rows: whale_holders (count of managers holding) and whale_net_action
    (sum of +1 add / -1 trim over each manager's latest visible quarter)."""
    filings = con.execute(
        """SELECT manager_cik, ticker, period, filed_at, change_status
           FROM filings_13f WHERE ticker IS NOT NULL AND filed_at IS NOT NULL"""
    ).df()
    if filings.empty:
        return pd.DataFrame(columns=["ticker", "week_end", "feature", "value"])
    filings["filed_at"] = pd.to_datetime(filings["filed_at"])
    filings["period"] = pd.to_datetime(filings["period"])
    universe = set(tickers)

    rows: list[tuple[str, pd.Timestamp, str, float]] = []
    for week in week_ends:
        visible = filings[filings["filed_at"] <= week]
        if visible.empty:
            continue
        # Each manager's most recent visible quarter.
        latest_period = visible.groupby("manager_cik")["period"].transform("max")
        current = visible[visible["period"] == latest_period]
        current = current[current["ticker"].isin(universe)]
        if current.empty:
            continue
        grouped = current.groupby("ticker")
        holders = grouped["manager_cik"].nunique()
        action = grouped["change_status"].apply(
            lambda s: float(sum(_ACTION_SCORE.get(x, 0.0) for x in s))
        )
        for ticker, count in holders.items():
            rows.append((str(ticker), week, "whale_holders", float(count)))
        for ticker, score in action.items():
            rows.append((str(ticker), week, "whale_net_action", score))
    return pd.DataFrame(rows, columns=["ticker", "week_end", "feature", "value"])


def insider_features(
    con: duckdb.DuckDBPyConnection,
    week_ends: pd.DatetimeIndex,
    tickers: list[str],
    window_days: int = 90,
) -> pd.DataFrame:
    """Long rows: insider_net_buys_90d (count of P minus S transactions) and
    insider_buy_value_90d (sum of purchase value) over a trailing window."""
    txns = con.execute(
        """SELECT ticker, tx_date, code, value_usd, filed_at
           FROM insider_form4 WHERE code IN ('P', 'S') AND tx_date IS NOT NULL"""
    ).df()
    if txns.empty:
        return pd.DataFrame(columns=["ticker", "week_end", "feature", "value"])
    txns["tx_date"] = pd.to_datetime(txns["tx_date"])
    txns["filed_at"] = pd.to_datetime(txns["filed_at"])
    txns = txns[txns["ticker"].isin(set(tickers))]

    rows: list[tuple[str, pd.Timestamp, str, float]] = []
    window = pd.Timedelta(days=window_days)
    for week in week_ends:
        vis = txns[
            (txns["filed_at"].fillna(txns["tx_date"]) <= week)
            & (txns["tx_date"] > week - window)
            & (txns["tx_date"] <= week)
        ]
        if vis.empty:
            continue
        for ticker, grp in vis.groupby("ticker"):
            buys = (grp["code"] == "P").sum()
            sells = (grp["code"] == "S").sum()
            buy_value = grp.loc[grp["code"] == "P", "value_usd"].fillna(0).sum()
            rows.append((str(ticker), week, "insider_net_buys_90d", float(buys - sells)))
            rows.append((str(ticker), week, "insider_buy_value_90d", float(buy_value)))
    return pd.DataFrame(rows, columns=["ticker", "week_end", "feature", "value"])
