"""Congress provider parsing and idempotent upsert."""

from __future__ import annotations

import httpx
import respx

from moi.ingest.congress import QuiverProvider, upsert_trades

SAMPLE = [
    {
        "Representative": "Nancy Pelosi",
        "House": "Representatives",
        "Ticker": "AVGO",
        "Transaction": "Purchase",
        "Range": "$1,000,001 - $5,000,000",
        "TransactionDate": "2026-06-01",
        "ReportDate": "2026-07-01",
    },
    {
        "Representative": "Some Senator",
        "House": "Senate",
        "Ticker": "VRT",
        "Transaction": "Sale (Full)",
        "Range": "$15,001 - $50,000",
        "TransactionDate": "2026-05-20",
        "ReportDate": "2026-06-15",
    },
]


def test_parse_row() -> None:
    trade = QuiverProvider.parse_row(SAMPLE[0])
    assert trade.politician == "Nancy Pelosi"
    assert trade.ticker == "AVGO"
    assert trade.direction == "buy"
    assert trade.tx_date.isoformat() == "2026-06-01"
    assert trade.disclosure_date.isoformat() == "2026-07-01"
    # 31-day disclosure lag is preserved, not collapsed.
    assert (trade.disclosure_date - trade.tx_date).days == 30


@respx.mock
def test_unsubscribed_key_skips_gracefully(db, monkeypatch) -> None:
    import moi.ingest.congress as mod

    respx.get(QuiverProvider.url).mock(
        return_value=httpx.Response(403, json={"detail": "Upgrade your subscription plan"})
    )
    monkeypatch.setattr(mod, "make_provider", lambda: QuiverProvider("free-tier-key"))
    assert mod.collect_congress(db) == 0  # no exception, no rows
    status = db.execute(
        "SELECT status, detail FROM run_log WHERE job = 'collect.congress'"
    ).fetchone()
    assert status[0] == "ok"
    assert "auth-insufficient" in status[1]


@respx.mock
def test_fetch_and_upsert_idempotent(db) -> None:
    respx.get(QuiverProvider.url).mock(return_value=httpx.Response(200, json=SAMPLE))
    provider = QuiverProvider("test-key")
    with httpx.Client() as client:
        trades = provider.fetch(client)
    assert len(trades) == 2
    assert {t.direction for t in trades} == {"buy", "sell"}

    upsert_trades(db, trades)
    upsert_trades(db, trades)  # same tx_id → no duplicates
    assert db.execute("SELECT count(*) FROM congress_trades").fetchone()[0] == 2


def test_tx_id_prefers_native_id_and_ignores_source() -> None:
    from moi.ingest.congress import CongressTrade

    base = dict(
        politician="Jane Doe",
        chamber="house",
        ticker="NVDA",
        direction="buy",
        amount_range="$1,001-$15,000",
        tx_date=None,
        disclosure_date=None,
    )
    # Same trade from two providers (no native id) → same tx_id (no duplication).
    a = CongressTrade(**base, source="quiver")
    b = CongressTrade(**base, source="unusualwhales")
    assert a.tx_id == b.tx_id
    # Two same-day same-band trades with distinct native ids → distinct rows.
    c = CongressTrade(**base, source="quiver", native_id="111")
    d = CongressTrade(**base, source="quiver", native_id="222")
    assert c.tx_id != d.tx_id
