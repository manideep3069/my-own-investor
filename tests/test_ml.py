"""ML pipeline tests: CV purging, leakage detection, labels, momentum features."""

from __future__ import annotations

import numpy as np
import pandas as pd

from moi.features.momentum import momentum_features
from moi.ml.cv import walk_forward_folds
from moi.ml.labels import forward_relative_returns
from moi.ml.ranker import DEFAULT_PARAMS, _fit_predict, weekly_rank_ic


def _weeks(n: int) -> list[pd.Timestamp]:
    return list(pd.date_range("2022-01-07", periods=n, freq="W-FRI"))


def test_walk_forward_purging() -> None:
    weeks = _weeks(120)
    folds = walk_forward_folds(weeks, min_train_weeks=52, test_span_weeks=4, embargo_weeks=13)
    assert folds
    for fold in folds:
        gap = (min(fold.test_weeks) - max(fold.train_weeks)).days // 7
        assert gap >= 13, "embargo violated: train too close to test"
        assert max(fold.train_weeks) < min(fold.test_weeks)


def test_labels_are_relative_and_forward() -> None:
    idx = pd.date_range("2024-01-05", periods=30, freq="W-FRI")
    closes = pd.DataFrame(
        {
            "A": np.linspace(100, 200, 30),
            "B": np.full(30, 100.0),
            "C": np.linspace(100, 80, 30),
        },
        index=idx,
    )
    labels = forward_relative_returns(closes, ["A", "B", "C"], horizon_weeks=13)
    # A rises fastest → positive relative label; C falls → negative.
    first = labels[labels["week_end"] == idx[0]].set_index("ticker")["label"]
    assert first["A"] > 0 > first["C"]
    # Last 13 weeks have no forward window → no labels.
    assert labels["week_end"].max() == idx[30 - 13 - 1]


def test_momentum_shapes() -> None:
    idx = pd.date_range("2024-01-05", periods=60, freq="W-FRI")
    closes = pd.DataFrame(
        {"X": np.linspace(50, 100, 60), "SMH": np.linspace(100, 120, 60)}, index=idx
    )
    dv = pd.DataFrame({"X": np.full(60, 1e7), "SMH": np.full(60, 1e9)}, index=idx)
    feats = momentum_features(closes, dv)
    assert feats["ret_13w"].loc[idx[13], "X"] > 0
    assert "rs_13w" in feats
    # X outperforms SMH in relative terms early on (steeper slope from lower base).
    assert feats["adv_dollar_13w_log"].loc[idx[20], "X"] == np.log10(1e7)


def _synthetic_panel(n_weeks: int = 140, n_tickers: int = 20, seed: int = 3):
    """Panel where one feature genuinely predicts the label."""
    rng = np.random.default_rng(seed)
    rows = []
    weeks = _weeks(n_weeks)
    for week in weeks:
        signal = rng.normal(size=n_tickers)
        noise = rng.normal(scale=0.5, size=n_tickers)
        label = signal + noise  # signal explains most of the cross-sectional variance
        for t_i in range(n_tickers):
            rows.append(
                {
                    "ticker": f"T{t_i}",
                    "week_end": week,
                    "alpha_feature": signal[t_i],
                    "noise_feature": rng.normal(),
                    "label": label[t_i],
                }
            )
    return pd.DataFrame(rows)


def test_signal_is_learned_and_shuffle_destroys_it() -> None:
    """The leakage canary: real signal → high OOS IC; shuffled labels → IC ≈ 0."""
    df = _synthetic_panel()
    feature_cols = ["alpha_feature", "noise_feature"]
    weeks = sorted(df["week_end"].unique())
    folds = walk_forward_folds(weeks, min_train_weeks=52, test_span_weeks=8, embargo_weeks=13)

    def run(frame: pd.DataFrame) -> float:
        preds = []
        for fold in folds:
            train = frame[frame["week_end"].isin(fold.train_weeks)]
            test = frame[frame["week_end"].isin(fold.test_weeks)]
            scores, _ = _fit_predict(train, test, feature_cols, DEFAULT_PARAMS)
            chunk = test[["ticker", "week_end", "label"]].copy()
            chunk["score"] = scores
            preds.append(chunk)
        ics = weekly_rank_ic(pd.concat(preds))
        return float(ics.mean())

    ic_real = run(df)
    shuffled = df.copy()
    rng = np.random.default_rng(11)
    shuffled["label"] = rng.permutation(shuffled["label"].to_numpy())
    ic_shuffled = run(shuffled)

    assert ic_real > 0.5, f"model failed to learn a genuine signal (IC={ic_real:.3f})"
    assert abs(ic_shuffled) < 0.1, f"IC survives label shuffling — leakage! ({ic_shuffled:.3f})"
