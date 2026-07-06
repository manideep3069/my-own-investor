"""Market-level features (macro, Polymarket, theme momentum).

Stored under the pseudo-ticker ``_MARKET_`` and joined onto every ticker row at
training time. As-of rule: last observed value at or before each week end.
"""

from __future__ import annotations

import duckdb
import pandas as pd

MARKET_TICKER = "_MARKET_"

_MACRO_FEATURES = {
    "T10Y2Y": "mkt_t10y2y",
    "BAMLH0A0HYM2": "mkt_hy_spread",
    "DGS10": "mkt_10y_yield",
}


def _asof_weekly(series: pd.Series, week_ends: pd.DatetimeIndex) -> pd.Series:
    """Resample an irregular series to week ends using the last value at or before."""
    s = series.sort_index()
    return s.reindex(s.index.union(week_ends)).ffill().reindex(week_ends)


def market_features(
    con: duckdb.DuckDBPyConnection,
    week_ends: pd.DatetimeIndex,
    closes: pd.DataFrame,
) -> pd.DataFrame:
    """Long rows (ticker=_MARKET_): macro levels/changes, Polymarket category
    probabilities, and theme momentum (SMH / SPY 13-week returns)."""
    frames: dict[str, pd.Series] = {}

    macro = con.execute("SELECT series_id, date, value FROM macro_series").df()
    if not macro.empty:
        macro["date"] = pd.to_datetime(macro["date"])
        for sid, feat in _MACRO_FEATURES.items():
            sub = macro[macro["series_id"] == sid].set_index("date")["value"]
            if sub.empty:
                continue
            weekly = _asof_weekly(sub, week_ends)
            frames[feat] = weekly
            frames[f"{feat}_13w_chg"] = weekly - weekly.shift(13)

    pm = con.execute(
        """SELECT m.category, s.ts, s.prob
           FROM polymarket_series s JOIN polymarket_markets m ON s.slug = m.slug"""
    ).df()
    if not pm.empty:
        pm["ts"] = pd.to_datetime(pm["ts"])
        for category, grp in pm.groupby("category"):
            daily = grp.groupby("ts")["prob"].mean()
            frames[f"mkt_pm_{category}"] = _asof_weekly(daily, week_ends)

    for bench in ("SMH", "SPY"):
        if bench in closes.columns:
            frames[f"mkt_{bench.lower()}_ret_13w"] = (
                closes[bench].pct_change(13, fill_method=None).reindex(week_ends)
            )

    rows = []
    for feat, series in frames.items():
        for week, value in series.dropna().items():
            rows.append((MARKET_TICKER, week, feat, float(value)))
    return pd.DataFrame(rows, columns=["ticker", "week_end", "feature", "value"])
