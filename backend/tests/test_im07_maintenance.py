"""Retention for a multi-year line agent: archive first, then delete.

These tests use throwaway SQLite files shaped like the real ones. They pin the
two properties that make a year of minute-by-minute telemetry safe: old rows are
copied before they are removed, and interrupted runs (which can be resumed) are
never pruned.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import scripts.im07_maintenance as maintenance


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _run_stamp(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def test_telemetry_is_archived_before_it_is_deleted(tmp_path, monkeypatch):
    results = tmp_path / "results.db"
    archive_dir = tmp_path / "archive"
    with sqlite3.connect(results) as connection:
        connection.execute(
            "CREATE TABLE line_telemetry (_row_id INTEGER PRIMARY KEY, _inserted_at TEXT, value TEXT)"
        )
        connection.execute(
            "INSERT INTO line_telemetry (_inserted_at, value) VALUES (?, 'old')",
            (_iso(400),),
        )
        connection.execute(
            "INSERT INTO line_telemetry (_inserted_at, value) VALUES (?, 'new')",
            (_iso(1),),
        )

    monkeypatch.setattr(maintenance, "RESULTS_DB", results)
    monkeypatch.setattr(maintenance, "ARCHIVE_DIR", archive_dir)

    result = maintenance._archive_telemetry(365, False)

    assert result == {"archived": 1}
    with sqlite3.connect(results) as connection:
        live = connection.execute("SELECT value FROM line_telemetry").fetchall()
    assert live == [("new",)]
    with sqlite3.connect(archive_dir / "line_telemetry_archive.db") as connection:
        archived = connection.execute(
            "SELECT value FROM line_telemetry"
        ).fetchall()
    assert archived == [("old",)]


def test_runs_and_checkpoints_are_pruned_but_interrupted_runs_survive(
    tmp_path, monkeypatch
):
    scaffold = tmp_path / "scaffold.db"
    checkpoints = tmp_path / "checkpoints.db"
    with sqlite3.connect(scaffold) as connection:
        connection.execute(
            "CREATE TABLE runs (id TEXT PRIMARY KEY, thread_id TEXT, status TEXT, created_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE run_steps (id INTEGER PRIMARY KEY, run_id TEXT)"
        )
        for run_id, thread_id, status, days in (
            ("old", "t-old", "succeeded", 120),
            ("paused", "t-paused", "interrupted", 120),
            ("recent", "t-recent", "succeeded", 1),
        ):
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?)",
                (run_id, thread_id, status, _run_stamp(days)),
            )
            connection.execute("INSERT INTO run_steps (run_id) VALUES (?)", (run_id,))

    with sqlite3.connect(checkpoints) as connection:
        connection.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)")
        connection.execute("CREATE TABLE writes (thread_id TEXT, value TEXT)")
        for thread_id in ("t-old", "t-paused", "t-recent"):
            connection.execute(
                "INSERT INTO checkpoints VALUES (?, 'c1')", (thread_id,)
            )
            connection.execute("INSERT INTO writes VALUES (?, 'w')", (thread_id,))

    monkeypatch.setattr(maintenance, "SCAFFOLD_DB", scaffold)
    monkeypatch.setattr(maintenance, "CHECKPOINTS_DB", checkpoints)

    result = maintenance._prune_runs(90, False)

    assert result["runs"] == 1
    with sqlite3.connect(scaffold) as connection:
        remaining = {
            row[0] for row in connection.execute("SELECT id FROM runs").fetchall()
        }
        steps = {
            row[0]
            for row in connection.execute("SELECT run_id FROM run_steps").fetchall()
        }
    assert remaining == {"paused", "recent"}
    assert steps == {"paused", "recent"}

    with sqlite3.connect(checkpoints) as connection:
        threads = {
            row[0] for row in connection.execute("SELECT thread_id FROM checkpoints")
        }
    assert threads == {"t-paused", "t-recent"}
