"""Results database CRUD: insert, update and delete with a required condition."""

from __future__ import annotations

import asyncio

import pytest

from app.tools import results_db
from app.tools.registry import ToolError


def _run(coro):
    return asyncio.run(coro)


def test_insert_update_delete_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(results_db, "db_path", lambda: tmp_path / "results.db")

    inserted = _run(
        results_db._db_write("items", [{"key": "a", "value": 1}, {"key": "b", "value": 2}])
    )
    assert inserted["inserted"] == 2

    updated = _run(
        results_db._db_update("items", {"value": 9}, "key", "a")
    )
    assert updated["updated"] == 1

    rows = _run(results_db._db_query("items"))
    by_key = {row["key"]: row["value"] for row in rows["rows"]}
    assert by_key == {"a": 9, "b": 2}

    deleted = _run(results_db._db_delete("items", "key", "b"))
    assert deleted["deleted"] == 1
    rows = _run(results_db._db_query("items"))
    assert [row["key"] for row in rows["rows"]] == ["a"]


def test_update_and_delete_require_a_where_condition(tmp_path, monkeypatch):
    monkeypatch.setattr(results_db, "db_path", lambda: tmp_path / "results.db")
    _run(results_db._db_write("items", [{"key": "a", "value": 1}]))

    with pytest.raises(ToolError):
        _run(results_db._db_update("items", {"value": 2}, "", None))
    with pytest.raises(ToolError):
        _run(results_db._db_delete("items", "", None))
