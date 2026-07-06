"""Helpers for recording pipeline runs in the ``run_log`` table."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import duckdb


def new_run_id() -> str:
    """Generate a short, sortable-ish unique run id."""
    return uuid.uuid4().hex[:12]


@dataclass
class RunHandle:
    """Mutable handle a job uses to report rows written / detail."""

    run_id: str
    job: str
    rows_written: int = 0
    detail: str = ""
    _extra: dict[str, object] = field(default_factory=dict)

    def add_rows(self, n: int) -> None:
        self.rows_written += n


@contextmanager
def track_run(
    con: duckdb.DuckDBPyConnection, job: str, run_id: str | None = None
) -> Iterator[RunHandle]:
    """Context manager that writes a ``run_log`` row and marks ok/error on exit."""
    rid = run_id or new_run_id()
    handle = RunHandle(run_id=rid, job=job)
    con.execute(
        "INSERT OR REPLACE INTO run_log (run_id, job, started_at, status) "
        "VALUES (?, ?, ?, 'running')",
        [rid, job, datetime.now()],
    )
    try:
        yield handle
    except Exception as exc:
        con.execute(
            """UPDATE run_log SET finished_at = ?, status = 'error', rows_written = ?, detail = ?
               WHERE run_id = ? AND job = ?""",
            [datetime.now(), handle.rows_written, f"{type(exc).__name__}: {exc}"[:500], rid, job],
        )
        raise
    else:
        con.execute(
            """UPDATE run_log SET finished_at = ?, status = 'ok', rows_written = ?, detail = ?
               WHERE run_id = ? AND job = ?""",
            [datetime.now(), handle.rows_written, handle.detail[:500], rid, job],
        )
