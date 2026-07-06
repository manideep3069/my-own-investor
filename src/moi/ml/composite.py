"""Rank-composite scorer — the production signal.

Zero fitted parameters: each week, tickers are percentile-ranked on a few features with
signs fixed by theory, and the ranks are averaged. Leakage-proof by construction and,
on this universe, decisively beats the LightGBM challenger out-of-sample
(IC ~0.07 t≈4 vs ~0.01; see docs/backtests/). The ML model stays available as a
challenger that must beat this composite before it can be promoted.

Signal rationale (per-feature weekly IC vs 13w forward relative return, 2020-2026):
    dist_52w_high      +0.060  proximity to 52-week high (anchoring/continuation)
    ret_52w            +0.045  long-horizon momentum
    ret_26w            +0.035  medium-horizon momentum
    adv_dollar_13w_log -0.038  smaller/less-traded names outperform within the theme
"""

from __future__ import annotations

import json
from datetime import datetime

import duckdb
import numpy as np
import pandas as pd

from moi.logging import get_logger
from moi.ml.dataset import load_dataset
from moi.ml.ranker import quartile_spread, weekly_rank_ic
from moi.runlog import new_run_id

log = get_logger(__name__)

# (feature, sign) — the composite averages sign * weekly percentile rank.
COMPOSITE_SPEC: list[tuple[str, float]] = [
    ("dist_52w_high", 1.0),
    ("ret_52w", 1.0),
    ("ret_26w", 1.0),
    ("adv_dollar_13w_log", -1.0),
]

WARMUP_WEEKS = 52  # ret_52w needs a year of history before ranks are meaningful


def composite_scores(
    df: pd.DataFrame, spec: list[tuple[str, float]] | None = None
) -> pd.DataFrame:
    """Score every (ticker, week) row. Returns columns: ticker, week_end, score, label."""
    spec = spec or COMPOSITE_SPEC
    ranks = [
        df.groupby("week_end")[feat].rank(pct=True) * sign
        for feat, sign in spec
        if feat in df.columns
    ]
    if not ranks:
        raise RuntimeError("No composite features present in dataset.")
    out = df[["ticker", "week_end", "label"]].copy()
    out["score"] = pd.concat(ranks, axis=1).mean(axis=1)
    out = out.dropna(subset=["score"])
    weeks = sorted(out["week_end"].unique())
    if len(weeks) > WARMUP_WEEKS:
        out = out[out["week_end"] >= weeks[WARMUP_WEEKS]]
    return out.reset_index(drop=True)


def evaluate_composite(con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, dict[str, float]]:
    """Score the full panel and compute IC metrics on the labeled portion."""
    df, _ = load_dataset(con)
    scores = composite_scores(df)
    labeled = scores.dropna(subset=["label"])
    ics = weekly_rank_ic(labeled)
    spreads = quartile_spread(labeled)
    metrics = {
        "rank_ic_mean": float(ics.mean()),
        "rank_ic_tstat": (
            float(ics.mean() / (ics.std() / np.sqrt(len(ics)))) if len(ics) > 2 else 0.0
        ),
        "quartile_spread_mean": float(spreads.mean()) if len(spreads) else float("nan"),
        "ic_positive_share": float((ics > 0).mean()) if len(ics) else float("nan"),
        "n_weeks": float(labeled["week_end"].nunique()),
    }
    run_id = new_run_id()
    con.execute(
        "INSERT INTO model_runs (run_id, created_at, kind, params, metrics) VALUES (?, ?, ?, ?, ?)",
        [
            run_id,
            datetime.now(),
            "composite",
            json.dumps({"spec": COMPOSITE_SPEC}),
            json.dumps(metrics),
        ],
    )
    log.info("composite_evaluated", **{k: round(v, 4) for k, v in metrics.items()})
    return scores, metrics


def latest_scores(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Composite scores for the most recent week — the live candidate ranking."""
    scores, _ = evaluate_composite(con)
    last_week = scores["week_end"].max()
    return (
        scores[scores["week_end"] == last_week]
        .sort_values("score", ascending=False)
        .reset_index(drop=True)
    )
