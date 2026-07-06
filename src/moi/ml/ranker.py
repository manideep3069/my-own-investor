"""Cross-sectional LightGBM ranker with purged walk-forward evaluation.

Outputs out-of-sample scores per (ticker, week) — the honest input for the backtest —
plus rank-IC / spread metrics, and stores a ``model_runs`` row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from moi.logging import get_logger
from moi.ml.cv import walk_forward_folds
from moi.ml.dataset import load_dataset
from moi.runlog import new_run_id

log = get_logger(__name__)

DEFAULT_PARAMS: dict[str, Any] = {
    # Small universe + short history → keep the model deliberately weak/regularized.
    "n_estimators": 300,
    "learning_rate": 0.03,
    "num_leaves": 15,
    "min_child_samples": 40,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.7,
    "reg_lambda": 5.0,
    "random_state": 7,
    "verbosity": -1,
}


@dataclass
class WalkForwardResult:
    run_id: str
    predictions: pd.DataFrame  # ticker, week_end, score, label
    metrics: dict[str, float]
    importances: pd.Series


def _fit_predict(
    train: pd.DataFrame, test: pd.DataFrame, feature_cols: list[str], params: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    from lightgbm import LGBMRegressor

    model = LGBMRegressor(**params)
    model.fit(train[feature_cols], train["label"])
    return model.predict(test[feature_cols]), model.feature_importances_


def weekly_rank_ic(preds: pd.DataFrame) -> pd.Series:
    """Spearman correlation between score and realized label, per week."""
    out = {}
    for week, grp in preds.groupby("week_end"):
        if len(grp) >= 8 and grp["label"].notna().all():
            ic = spearmanr(grp["score"], grp["label"]).statistic
            if ic == ic:  # not NaN
                out[week] = float(ic)
    return pd.Series(out).sort_index()


def quartile_spread(preds: pd.DataFrame) -> pd.Series:
    """Top-quartile minus bottom-quartile mean label, per week."""
    out = {}
    for week, grp in preds.groupby("week_end"):
        if len(grp) < 12 or grp["label"].isna().any():
            continue
        q = max(3, len(grp) // 4)
        ranked = grp.sort_values("score")
        out[week] = float(ranked.tail(q)["label"].mean() - ranked.head(q)["label"].mean())
    return pd.Series(out).sort_index()


def train_walkforward(
    con: duckdb.DuckDBPyConnection,
    params: dict[str, Any] | None = None,
    *,
    min_train_weeks: int = 52,
    test_span_weeks: int = 4,
    embargo_weeks: int = 13,
) -> WalkForwardResult:
    """Run the full purged walk-forward loop and persist metrics."""
    params = {**DEFAULT_PARAMS, **(params or {})}
    df, feature_cols = load_dataset(con)
    labeled = df.dropna(subset=["label"])
    weeks = sorted(labeled["week_end"].unique())
    folds = walk_forward_folds(
        list(weeks),
        min_train_weeks=min_train_weeks,
        test_span_weeks=test_span_weeks,
        embargo_weeks=embargo_weeks,
    )
    if not folds:
        raise RuntimeError(f"Not enough labeled weeks ({len(weeks)}) for walk-forward CV.")

    all_preds: list[pd.DataFrame] = []
    importance_sum = np.zeros(len(feature_cols))
    for fold in folds:
        train = labeled[labeled["week_end"].isin(fold.train_weeks)]
        test = labeled[labeled["week_end"].isin(fold.test_weeks)]
        if train.empty or test.empty:
            continue
        scores, importances = _fit_predict(train, test, feature_cols, params)
        importance_sum += importances
        chunk = test[["ticker", "week_end", "label"]].copy()
        chunk["score"] = scores
        all_preds.append(chunk)

    preds = pd.concat(all_preds, ignore_index=True)
    ics = weekly_rank_ic(preds)
    spreads = quartile_spread(preds)
    metrics = {
        "n_folds": float(len(folds)),
        "n_oos_weeks": float(preds["week_end"].nunique()),
        "rank_ic_mean": float(ics.mean()),
        "rank_ic_tstat": (
            float(ics.mean() / (ics.std() / np.sqrt(len(ics)))) if len(ics) > 2 else 0.0
        ),
        "quartile_spread_mean": float(spreads.mean()) if len(spreads) else float("nan"),
        "ic_positive_share": float((ics > 0).mean()) if len(ics) else float("nan"),
    }
    importances = pd.Series(importance_sum, index=feature_cols).sort_values(ascending=False)

    run_id = new_run_id()
    con.execute(
        "INSERT INTO model_runs (run_id, created_at, kind, params, metrics) VALUES (?, ?, ?, ?, ?)",
        [run_id, datetime.now(), "walkforward", json.dumps(params), json.dumps(metrics)],
    )
    log.info("walkforward_done", **{k: round(v, 4) for k, v in metrics.items()})
    return WalkForwardResult(
        run_id=run_id, predictions=preds, metrics=metrics, importances=importances
    )
