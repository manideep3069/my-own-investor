-- Phase 2 schema: weekly feature store and model/backtest run registries.

-- Long-format weekly features, point-in-time correct: every value is computed using
-- only data available on `week_end` (a Friday). Ticker '_MARKET_' rows hold
-- market-level features (macro, Polymarket, regime inputs) joined at training time.
CREATE TABLE IF NOT EXISTS features_weekly (
    ticker   VARCHAR NOT NULL,
    week_end DATE    NOT NULL,
    feature  VARCHAR NOT NULL,
    value    DOUBLE,
    PRIMARY KEY (ticker, week_end, feature)
);

-- One row per training/evaluation run: hyperparams + walk-forward metrics as JSON.
CREATE TABLE IF NOT EXISTS model_runs (
    run_id     VARCHAR NOT NULL PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    kind       VARCHAR NOT NULL,      -- "walkforward" | "final"
    params     VARCHAR,               -- JSON
    metrics    VARCHAR                -- JSON (rank IC, decile spread, ...)
);

-- One row per backtest: config + performance vs baselines as JSON.
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id     VARCHAR NOT NULL PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    config     VARCHAR,               -- JSON
    metrics    VARCHAR                -- JSON
);
