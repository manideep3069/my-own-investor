"""Momentum / technical features from the weekly close panel. All trailing-window."""

from __future__ import annotations

import numpy as np
import pandas as pd


def momentum_features(
    closes: pd.DataFrame, dollar_vol: pd.DataFrame, benchmark: str = "SMH"
) -> dict[str, pd.DataFrame]:
    """Compute per-ticker weekly momentum panels keyed by feature name.

    Args:
        closes: weekly close panel (index week_end, columns tickers).
        dollar_vol: weekly mean daily dollar-volume panel.
        benchmark: ticker used for relative strength.
    """
    rets = closes.pct_change(fill_method=None)
    out: dict[str, pd.DataFrame] = {}

    for weeks in (4, 13, 26, 52):
        out[f"ret_{weeks}w"] = closes.pct_change(weeks, fill_method=None)

    out["vol_13w"] = rets.rolling(13).std()
    out["dist_52w_high"] = closes / closes.rolling(52).max() - 1.0

    if benchmark in closes.columns:
        bench_13w = closes[benchmark].pct_change(13, fill_method=None)
        out["rs_13w"] = out["ret_13w"].sub(bench_13w, axis=0)

    with np.errstate(divide="ignore", invalid="ignore"):
        # A zero-volume window would yield log10(0) = -inf, which survives dropna and
        # poisons ranks downstream — map infinities to NaN.
        out["adv_dollar_13w_log"] = np.log10(dollar_vol.rolling(13).mean()).replace(
            [np.inf, -np.inf], np.nan
        )
    out["vol_trend"] = dollar_vol.rolling(4).mean() / dollar_vol.rolling(26).mean()

    return out
