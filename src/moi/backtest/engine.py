"""Weekly-rebalance backtest over out-of-sample ranker scores.

Deliberately simple and transparent (no vectorbt dependency): equal-weight top-N by
score, rebalanced every ``rebalance_weeks``, with per-side transaction costs charged on
turnover. Scores at week t select the portfolio held over (t, t+1, ...] — no look-ahead:
the score at t was produced by a model trained only on data before t (walk-forward).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise

import duckdb
import numpy as np
import pandas as pd

from moi.features.panels import weekly_close_panel
from moi.logging import get_logger
from moi.runlog import new_run_id
from moi.universe import candidate_tickers

log = get_logger(__name__)


@dataclass
class BacktestConfig:
    top_n: int = 10
    rebalance_weeks: int = 4
    cost_bps_per_side: float = 15.0  # commission + half-spread estimate for liquid names


@dataclass
class BacktestResult:
    run_id: str
    config: BacktestConfig
    strategy_returns: pd.Series  # weekly, net of costs
    baselines: dict[str, pd.Series]
    metrics: dict[str, dict[str, float]]
    holdings_log: list[tuple[str, list[str]]] = field(default_factory=list)


def performance_metrics(weekly_returns: pd.Series) -> dict[str, float]:
    r = weekly_returns.dropna()
    if len(r) == 0:
        return {}
    cumulative = (1 + r).cumprod()
    total = float(cumulative.iloc[-1] - 1)
    ann_return = float(cumulative.iloc[-1] ** (52 / len(r)) - 1)
    ann_vol = float(r.std() * np.sqrt(52))
    sharpe = ann_return / ann_vol if ann_vol > 0 else 0.0
    drawdown = cumulative / cumulative.cummax() - 1
    return {
        "total_return": total,
        "ann_return": ann_return,
        "ann_vol": ann_vol,
        "sharpe": float(sharpe),
        "max_drawdown": float(drawdown.min()),
        "n_weeks": float(len(r)),
    }


def run_backtest(
    con: duckdb.DuckDBPyConnection,
    predictions: pd.DataFrame,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Simulate the strategy over the OOS prediction window and compare to baselines."""
    cfg = config or BacktestConfig()
    closes = weekly_close_panel(con)
    weekly_rets = closes.pct_change(fill_method=None)
    tickers = [t for t in candidate_tickers() if t in closes.columns]

    pred_weeks = sorted(predictions["week_end"].unique())
    holdings: list[str] = []
    strat_returns: dict[pd.Timestamp, float] = {}
    holdings_log: list[tuple[str, list[str]]] = []
    weeks_since_rebalance = cfg.rebalance_weeks  # force rebalance on first scored week

    all_weeks = [w for w in weekly_rets.index if pred_weeks[0] <= w <= pred_weeks[-1]]
    scores_by_week = {w: g for w, g in predictions.groupby("week_end")}

    for week, next_week in pairwise(all_weeks):
        cost = 0.0
        if weeks_since_rebalance >= cfg.rebalance_weeks and week in scores_by_week:
            ranked = scores_by_week[week].sort_values("score", ascending=False)
            new_holdings = [t for t in ranked["ticker"].tolist() if t in tickers][: cfg.top_n]
            if new_holdings:
                changed = (
                    len(set(new_holdings) ^ set(holdings)) / 2 if holdings else len(new_holdings)
                )
                turnover = changed / max(len(new_holdings), 1)
                cost = 2 * turnover * cfg.cost_bps_per_side / 10_000  # sell + buy legs
                holdings = new_holdings
                holdings_log.append((str(pd.Timestamp(week).date()), list(holdings)))
                weeks_since_rebalance = 0
        weeks_since_rebalance += 1
        if not holdings:
            continue
        # Return realized over the following week, equal-weighted across holdings.
        realized = weekly_rets.loc[next_week, holdings].dropna()
        strat_returns[next_week] = float(realized.mean()) - cost if len(realized) else -cost

    strategy = pd.Series(strat_returns).sort_index()

    baselines: dict[str, pd.Series] = {
        "equal_weight_universe": weekly_rets.loc[strategy.index, tickers].mean(axis=1),
    }
    for bench in ("SMH", "SPY"):
        if bench in weekly_rets.columns:
            baselines[bench] = weekly_rets.loc[strategy.index, bench]

    metrics = {"strategy": performance_metrics(strategy)}
    for name, series in baselines.items():
        metrics[name] = performance_metrics(series)

    run_id = new_run_id()
    con.execute(
        "INSERT INTO backtest_runs (run_id, created_at, config, metrics) VALUES (?, ?, ?, ?)",
        [run_id, datetime.now(), json.dumps(cfg.__dict__), json.dumps(metrics)],
    )
    log.info("backtest_done", run_id=run_id, sharpe=round(metrics["strategy"].get("sharpe", 0), 3))
    return BacktestResult(
        run_id=run_id,
        config=cfg,
        strategy_returns=strategy,
        baselines=baselines,
        metrics=metrics,
        holdings_log=holdings_log,
    )


def render_report(
    result: BacktestResult, model_metrics: dict[str, float], importances: pd.Series
) -> str:
    """Markdown report for docs/backtests/."""
    lines = [
        "# Backtest report",
        "",
        f"- run_id: `{result.run_id}`",
        f"- config: top_n={result.config.top_n}, rebalance={result.config.rebalance_weeks}w, "
        f"cost={result.config.cost_bps_per_side}bps/side",
        "",
        "## Model (walk-forward, out-of-sample)",
        "",
    ]
    lines += [f"- {k}: {v:.4f}" for k, v in model_metrics.items()]
    lines += [
        "",
        "## Performance (net of costs)",
        "",
        "| series | total | ann | vol | sharpe | maxDD |",
        "|---|---|---|---|---|---|",
    ]

    def row(name: str, m: dict[str, float]) -> str:
        if not m:
            return f"| {name} | - | - | - | - | - |"
        return (
            f"| {name} | {m['total_return']:+.1%} | {m['ann_return']:+.1%} "
            f"| {m['ann_vol']:.1%} | {m['sharpe']:.2f} | {m['max_drawdown']:.1%} |"
        )

    lines.append(row("**strategy**", result.metrics["strategy"]))
    for name in result.baselines:
        lines.append(row(name, result.metrics[name]))

    lines += ["", "## Top features (LightGBM importance)", ""]
    lines += [f"- {feat}: {imp:.0f}" for feat, imp in importances.head(12).items()]
    lines += ["", "## Holdings by rebalance", ""]
    lines += [f"- {week}: {', '.join(hold)}" for week, hold in result.holdings_log[-12:]]
    return "\n".join(lines) + "\n"


def gate_passed(metrics: dict[str, dict[str, float]]) -> bool:
    """Phase 2 gate: strategy Sharpe must beat the equal-weight universe after costs."""
    strat = metrics.get("strategy", {})
    ew = metrics.get("equal_weight_universe", {})
    return bool(strat) and bool(ew) and strat["sharpe"] > ew["sharpe"]
