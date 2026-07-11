"""Tests for moi.ops — status checks and dashboard job control."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from moi import ops


# --------------------------------------------------------------------------- #
# Job registry
# --------------------------------------------------------------------------- #
def test_jobs_registry_is_consistent() -> None:
    for key, spec in ops.JOBS.items():
        assert spec.key == key
        assert spec.args, key
        assert spec.label and spec.blurb


# --------------------------------------------------------------------------- #
# Job lifecycle (real subprocess: `python -m moi --help`)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def job_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(ops, "JOB_LOG_DIR", tmp_path / "joblogs")
    monkeypatch.setattr(ops, "CURRENT_JOB_FILE", tmp_path / "joblogs" / "current-job.json")
    return tmp_path


def test_job_lifecycle(job_dirs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ops.JOBS, "help", ops.JobSpec("help", "Help", ("--help",), "prints usage"))
    assert ops.current_job() is None

    info = ops.start_job("help")
    assert info["key"] == "help"

    for _ in range(100):  # `moi --help` should exit within seconds
        cur = ops.current_job()
        assert cur is not None
        if not cur["running"]:
            break
        time.sleep(0.1)
    else:
        pytest.fail("job did not finish")

    log = ops.tail_log(Path(info["log"]))
    assert "moi" in log  # typer usage text

    ops.clear_job()
    assert ops.current_job() is None


def test_current_job_handles_corrupt_state(job_dirs: Path) -> None:
    ops.CURRENT_JOB_FILE.parent.mkdir(parents=True, exist_ok=True)
    ops.CURRENT_JOB_FILE.write_text("not json")
    assert ops.current_job() is None


def test_pid_alive_for_dead_pid(job_dirs: Path) -> None:
    ops.CURRENT_JOB_FILE.parent.mkdir(parents=True, exist_ok=True)
    ops.CURRENT_JOB_FILE.write_text(
        json.dumps({"pid": 99_999_999, "key": "run", "log": "x", "started": "now"})
    )
    cur = ops.current_job()
    assert cur is not None
    assert cur["running"] is False


def test_tail_log_missing_file(tmp_path: Path) -> None:
    assert ops.tail_log(tmp_path / "nope.log") == ""


# --------------------------------------------------------------------------- #
# Connection checks
# --------------------------------------------------------------------------- #
def test_connection_checks_cover_every_dependency() -> None:
    checks = ops.connection_checks()
    names = {c.name for c in checks}
    assert {
        "Database",
        "IBKR TWS",
        "SEC EDGAR",
        "FRED macro",
        "Congress trades",
        "Polymarket",
        "Claude agents",
        "Telegram",
        "Scheduler",
        "Trading mode",
    } <= names
    assert all(c.state in {"ok", "warn", "off", "error"} for c in checks)
    assert all(c.detail for c in checks)


# --------------------------------------------------------------------------- #
# Source board
# --------------------------------------------------------------------------- #
def test_source_board_merges_freshness_and_runs(db: duckdb.DuckDBPyConnection) -> None:
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) "
        "VALUES ('AAPL', current_date, 100.0, 'test')"
    )
    db.execute(
        """INSERT INTO run_log (run_id, job, started_at, finished_at, status, rows_written, detail)
           VALUES ('r1', 'collect.prices', ?, ?, 'ok', 42, 'yfinance')""",
        [datetime(2026, 7, 10, 22, 0), datetime(2026, 7, 10, 22, 5)],
    )
    board = ops.source_board(db)
    by_source = {row["source"]: row for row in board}
    prices = by_source["prices"]
    assert prices["freshness"] == "ok"
    assert prices["last run"] == "ok"
    assert prices["ran at"].startswith("2026-07-10")
    # A source that never ran reports "never", not an error.
    assert by_source["news"]["last run"] == "never"
