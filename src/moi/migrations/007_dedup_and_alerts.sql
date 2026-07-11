-- 007: dedup-key migrations + alert dedup state.
--
-- news_items ids now hash (url, ticker) instead of url alone, and congress_trades
-- ids now prefer the provider's native transaction id (and no longer include the
-- provider name). Rows stored under the old scheme would duplicate on the next
-- collect, so both tables are cleared — they refill from their sources on the
-- next run (news is ephemeral; congress providers serve full history).
DELETE FROM news_items;
DELETE FROM congress_trades;

-- Sent-alert journal so the urgent watcher can suppress repeats
-- (same alert re-firing daily trains the reader to ignore it).
CREATE TABLE IF NOT EXISTS alerts_sent (
    kind      VARCHAR NOT NULL,   -- big_move | whale_filing | data_quality | ...
    key       VARCHAR NOT NULL,   -- e.g. ticker+date, accession, table name
    last_sent TIMESTAMP NOT NULL,
    PRIMARY KEY (kind, key)
);
