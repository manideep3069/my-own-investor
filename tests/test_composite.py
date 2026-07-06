"""Composite scorer: ranks respect signs, warmup dropped, no fitted state."""

from __future__ import annotations

import numpy as np
import pandas as pd

from moi.ml.composite import composite_scores


def _panel(n_weeks: int = 60) -> pd.DataFrame:
    weeks = pd.date_range("2023-01-06", periods=n_weeks, freq="W-FRI")
    rows = []
    for week in weeks:
        # HI is close to its high with strong momentum and small size; LO is the opposite.
        rows.append(
            {
                "ticker": "HI",
                "week_end": week,
                "dist_52w_high": -0.02,
                "ret_52w": 0.8,
                "ret_26w": 0.3,
                "adv_dollar_13w_log": 6.5,
                "label": np.nan,
            }
        )
        rows.append(
            {
                "ticker": "MID",
                "week_end": week,
                "dist_52w_high": -0.2,
                "ret_52w": 0.2,
                "ret_26w": 0.1,
                "adv_dollar_13w_log": 7.5,
                "label": np.nan,
            }
        )
        rows.append(
            {
                "ticker": "LO",
                "week_end": week,
                "dist_52w_high": -0.5,
                "ret_52w": -0.3,
                "ret_26w": -0.2,
                "adv_dollar_13w_log": 9.0,
                "label": np.nan,
            }
        )
    return pd.DataFrame(rows)


def test_composite_orders_by_signal() -> None:
    scores = composite_scores(_panel())
    last = scores[scores["week_end"] == scores["week_end"].max()]
    ordered = last.sort_values("score", ascending=False)["ticker"].tolist()
    assert ordered == ["HI", "MID", "LO"]


def test_composite_drops_warmup() -> None:
    scores = composite_scores(_panel(n_weeks=60))
    assert scores["week_end"].nunique() == 60 - 52


def test_composite_deterministic() -> None:
    a = composite_scores(_panel())
    b = composite_scores(_panel())
    pd.testing.assert_frame_equal(a, b)
