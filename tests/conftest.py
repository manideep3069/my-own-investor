"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from moi.db import connect


@pytest.fixture()
def db(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    """A fresh migrated DuckDB in a temp dir."""
    con = connect(db_path=tmp_path / "test.duckdb")
    yield con
    con.close()
