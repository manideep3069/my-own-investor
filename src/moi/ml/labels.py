"""Labels: forward relative return.

Label(t) = ticker's return over (t, t+H] minus the cross-sectional median return of the
universe over the same window. Relative labels make the task cross-sectional ranking
(pick winners *within* the theme) rather than market timing.
"""

from __future__ import annotations

import pandas as pd

HORIZON_WEEKS = 13


def forward_relative_returns(
    closes: pd.DataFrame, tickers: list[str], horizon_weeks: int = HORIZON_WEEKS
) -> pd.DataFrame:
    """Long rows (ticker, week_end, label). Weeks lacking full forward data are dropped."""
    cols = [t for t in tickers if t in closes.columns]
    px = closes[cols]
    fwd = px.shift(-horizon_weeks) / px - 1.0
    rel = fwd.sub(fwd.median(axis=1), axis=0)
    long = rel.stack().reset_index()
    long.columns = ["week_end", "ticker", "label"]
    return long[["ticker", "week_end", "label"]].dropna(subset=["label"])
