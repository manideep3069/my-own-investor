"""Purged walk-forward cross-validation for overlapping-label panel data.

The label at week t uses returns over (t, t+H]. If a training week's label window
overlaps the test period, information leaks backward. The embargo drops the last H
weeks before each test window from training (purging), per López de Prado.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Fold:
    train_weeks: list[pd.Timestamp]
    test_weeks: list[pd.Timestamp]


def walk_forward_folds(
    weeks: list[pd.Timestamp],
    *,
    min_train_weeks: int = 52,
    test_span_weeks: int = 4,
    embargo_weeks: int = 13,
) -> list[Fold]:
    """Expanding-window folds over sorted unique weeks.

    Fold k tests weeks [s, s+span) and trains on all weeks < s - embargo.
    """
    weeks = sorted(weeks)
    folds: list[Fold] = []
    start = min_train_weeks + embargo_weeks
    for s in range(start, len(weeks), test_span_weeks):
        test = weeks[s : s + test_span_weeks]
        train = weeks[: s - embargo_weeks]
        if not test or len(train) < min_train_weeks:
            continue
        folds.append(Fold(train_weeks=list(train), test_weeks=list(test)))
    return folds
