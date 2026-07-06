-- IBKR order ids are session-scoped; permId is the durable identifier needed to
-- reconcile fills after TWS restarts.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS perm_id BIGINT;
