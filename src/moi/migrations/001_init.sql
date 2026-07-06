-- Phase 0 schema: universe membership, daily prices, and a run log.
-- Later phases add their own migrations (002_..., 003_...).

-- Point-in-time universe membership. `first_seen`/`last_seen` let backtests reconstruct
-- which tickers were in the universe on any given date (avoids survivorship bias).
CREATE TABLE IF NOT EXISTS universe (
    ticker      VARCHAR NOT NULL,
    name        VARCHAR,
    sub_sector  VARCHAR,
    is_benchmark BOOLEAN NOT NULL DEFAULT FALSE,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen  DATE,
    last_seen   DATE,
    PRIMARY KEY (ticker)
);

-- Daily OHLCV. Natural key = (ticker, date). `source` records provenance
-- (ibkr | yfinance) so we can prefer one over the other later.
CREATE TABLE IF NOT EXISTS prices_daily (
    ticker      VARCHAR NOT NULL,
    date        DATE    NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    adj_close   DOUBLE,
    volume      BIGINT,
    source      VARCHAR NOT NULL,
    PRIMARY KEY (ticker, date)
);

-- One row per collector/pipeline invocation for auditability and freshness checks.
CREATE TABLE IF NOT EXISTS run_log (
    run_id      VARCHAR NOT NULL,
    job         VARCHAR NOT NULL,   -- e.g. "collect.prices", "ibkr.ping"
    started_at  TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    status      VARCHAR NOT NULL,   -- "running" | "ok" | "error"
    rows_written BIGINT DEFAULT 0,
    detail      VARCHAR,
    PRIMARY KEY (run_id, job)
);
