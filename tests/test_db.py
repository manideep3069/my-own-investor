"""Migrations apply once and create the expected schema."""

from __future__ import annotations

from pathlib import Path

from moi.db import connect, migrate


def test_migrations_create_tables(db) -> None:
    tables = {r[0] for r in db.execute("SHOW TABLES").fetchall()}
    assert {"universe", "prices_daily", "run_log", "schema_version"} <= tables


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    con = connect(db_path=tmp_path / "m.duckdb")
    # Already migrated on connect; a second call applies nothing.
    assert migrate(con) == 0
    versions = con.execute("SELECT count(*) FROM schema_version").fetchone()[0]
    assert versions >= 1
