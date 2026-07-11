"""Load the seed universe from ``config/universe.yaml`` and sync it into the DB."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import yaml

from moi.config import CONFIG_DIR
from moi.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Instrument:
    ticker: str
    name: str | None
    sub_sector: str | None
    is_benchmark: bool


def load_universe(path: Path | None = None) -> list[Instrument]:
    """Parse the universe YAML into a flat list of instruments (candidates + benchmarks)."""
    p = path or (CONFIG_DIR / "universe.yaml")
    data = yaml.safe_load(p.read_text()) or {}

    instruments: list[Instrument] = []
    for entry in data.get("benchmarks", []) or []:
        instruments.append(
            Instrument(
                ticker=entry["ticker"].upper(),
                name=entry.get("name"),
                sub_sector=None,
                is_benchmark=True,
            )
        )
    for sub_sector, entries in (data.get("sub_sectors") or {}).items():
        for entry in entries or []:
            instruments.append(
                Instrument(
                    ticker=entry["ticker"].upper(),
                    name=entry.get("name"),
                    sub_sector=sub_sector,
                    is_benchmark=False,
                )
            )

    # Guard against accidental duplicates across sub-sectors.
    seen: set[str] = set()
    for inst in instruments:
        if inst.ticker in seen:
            raise ValueError(f"Duplicate ticker in universe.yaml: {inst.ticker}")
        seen.add(inst.ticker)
    return instruments


def candidate_tickers(path: Path | None = None) -> list[str]:
    """Return only the tradable candidate tickers (excludes benchmarks)."""
    return [i.ticker for i in load_universe(path) if not i.is_benchmark]


def all_tickers(path: Path | None = None) -> list[str]:
    """Return every ticker we need prices for (candidates + benchmarks)."""
    return [i.ticker for i in load_universe(path)]


def sync_universe(con: duckdb.DuckDBPyConnection, path: Path | None = None) -> int:
    """Upsert the seed universe into the ``universe`` table (idempotent).

    Sets ``first_seen`` on insert, refreshes ``last_seen`` and metadata every run, and
    marks tickers no longer in the YAML as ``active = FALSE`` (keeps history for backtests).
    """
    instruments = load_universe(path)
    if not instruments:
        # An empty YAML would deactivate everything (and `NOT IN ()` is a parse error);
        # this is always a config mistake, so fail loudly instead.
        raise ValueError("config/universe.yaml contains no instruments — refusing to sync")
    today = date.today()
    tickers = [i.ticker for i in instruments]

    for inst in instruments:
        con.execute(
            """
            INSERT INTO universe
                (ticker, name, sub_sector, is_benchmark, active, first_seen, last_seen)
            VALUES (?, ?, ?, ?, TRUE, ?, ?)
            ON CONFLICT (ticker) DO UPDATE SET
                name = excluded.name,
                sub_sector = excluded.sub_sector,
                is_benchmark = excluded.is_benchmark,
                active = TRUE,
                last_seen = excluded.last_seen
            """,
            [inst.ticker, inst.name, inst.sub_sector, inst.is_benchmark, today, today],
        )

    # Deactivate anything previously tracked but now removed from the YAML.
    placeholders = ", ".join("?" for _ in tickers)
    con.execute(
        f"UPDATE universe SET active = FALSE WHERE ticker NOT IN ({placeholders})",
        tickers,
    )
    log.info("universe_synced", count=len(instruments))
    return len(instruments)
