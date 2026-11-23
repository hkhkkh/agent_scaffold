"""Results database tools — somewhere for an agent to put what it found.

Why this is a *tool* and not a node
-----------------------------------

The obvious shape would be a "Database" node. It was rejected, and the reason is
worth writing down because it is the same reason three other things in this
codebase are shaped the way they are.

The Code node — the only other place a workflow could open a database — runs
inside :mod:`app.runtime.sandbox`, which **removes ``open`` from the builtins**
and imports from a fixed module allowlist that does not include ``sqlite3``. So a
Code node cannot open a database, by construction and on purpose.

That leaves the tool layer, which is where the platform's own reach already
lives: ``http_request`` opens sockets, ``web_search`` scrapes search engines.
"Write a row to a table" is the same kind of capability — a thing the deployment
grants, that the graph composes — so it is registered the same way. A workflow
reaches it through the Tool node (deterministic, no model in the loop) or the
Agent node (the model decides when to record something).

Which table, and why it is a file
---------------------------------

One SQLite file, ``<data_dir>/results.db``, beside the platform's own
``scaffold.db``. Separate file rather than a separate schema inside the platform
database, for a reason that is about ownership rather than tidiness: this data
belongs to *a workflow*, not to the platform. Dropping it, inspecting it with
``sqlite3``, or shipping it somewhere must not require touching the tables the
run history lives in.

The schema is inferred per call. A tool that demanded a migration step would be
unusable from a canvas — the author is drawing a graph, not writing DDL — so
``db_write`` creates the table on first use and adds any column it has not seen
before. Inference is deliberately shallow: JSON scalars map to SQLite's own
storage classes (``INTEGER`` / ``REAL`` / ``TEXT``) and everything else is stored
as JSON text. Values are bound as parameters in every statement; identifiers are
quoted and validated, because a table name cannot be a bound parameter and
``db_write``'s ``table`` argument can come from a model.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from app.settings import get_settings
from app.tools.registry import ToolDef, ToolError, register_tool

__all__ = ["RESULTS_DB_FILENAME", "db_path", "snapshot"]

#: The file's name inside the data directory. Named here rather than built
#: inline at each use, so the writer and the read-only snapshot endpoint cannot
#: drift onto two different files.
RESULTS_DB_FILENAME = "results.db"

#: Bookkeeping columns. Prefixed with an underscore so they sort away from the
#: agent's own fields in a SELECT *, and so a payload that happens to contain
#: "inserted_at" cannot collide with them.
_ROW_ID = "_row_id"
_INSERTED_AT = "_inserted_at"

#: Type name a column gets when its first observed value was a string.
_DEFAULT_TEXT = "TEXT"

#: A table or column name as this module will accept it. Deliberately narrower
#: than SQLite's own rules: the point is not to accept every legal identifier but
#: to make it impossible to hand a statement a name carrying a quote, a comment
#: marker, or a second statement.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def db_path() -> Path:
    """Absolute path of the results database. Resolved per call, not cached.

    Per call because ``data_dir`` is configuration and a cached path would keep
    pointing at a directory the process has already been told to stop using.
    """
    return get_settings().data_dir / RESULTS_DB_FILENAME


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    # WAL so the read-only snapshot endpoint can read while a run is writing.
    # Under SQLite's default rollback journal a reader blocks the writer, and the
    # live panel polls once a second — that collision would show up as a run that
    # mysteriously stalls whenever someone is watching it.
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _check_identifier(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER.match(text):
        raise ToolError(
            f"'{text}' is not a usable {what} name. Use letters, digits and "
            "underscores, starting with a letter or underscore."
        )
    return text


def _quote(name: str) -> str:
    """Quote a validated identifier for interpolation into a statement."""
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------


def _column_type(value: Any) -> str:
    """The SQLite column type for the first value seen in a column.

    ``bool`` is checked before ``int`` because ``bool`` is a subclass of it and
    the narrower answer is the more useful one. Everything that is not a scalar
    is stored as JSON text — including ``None``, which therefore does not pin a
    column to a type it may not deserve on the next call.
    """
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    return _DEFAULT_TEXT


def _as_stored(value: Any) -> Any:
    """A Python value as something sqlite3 can bind.

    Containers become JSON text so a nested object survives the round trip; the
    snapshot endpoint parses them back. ``bool`` becomes 0/1 because sqlite3
    refuses it outright (``InterfaceError``), which is a failure worth handling
    here rather than discovering on the first agent that records a flag.
    """
    if value is None or isinstance(value, (str, int, float, bytes)):
        if isinstance(value, bool):
            return int(value)
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# Synchronous core — every statement in this module lives here
# ---------------------------------------------------------------------------
#
# The tool functions below wrap these in `asyncio.to_thread`. sqlite3 is a
# blocking library, and calling it directly from a coroutine would stall the
# event loop for every other run on the process — which is the failure the whole
# concurrency design exists to avoid. Keeping the SQL in plain functions also
# means the snapshot endpoint and the tools share one implementation instead of
# two that must be kept in step.


def _ensure_columns(
    connection: sqlite3.Connection, table: str, rows: list[dict[str, Any]]
) -> list[str]:
    """Create ``table`` if needed and add any column it does not have yet.

    Returns the columns actually added, so the caller can report them. A column
    is typed from the first value that arrives for it and never re-typed
    afterwards: SQLite's type affinity would let a later value change what is
    stored anyway, and rewriting a table on every call would make a write cost
    grow with the table's size.
    """
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {_quote(table)} ("
        f"{_quote(_ROW_ID)} INTEGER PRIMARY KEY AUTOINCREMENT, "
        f"{_quote(_INSERTED_AT)} TEXT NOT NULL)"
    )
    existing = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
    }

    added: list[str] = []
    for row in rows:
        for key, value in row.items():
            column = _check_identifier(key, "column")
            if column in existing:
                continue
            connection.execute(
                f"ALTER TABLE {_quote(table)} "
                f"ADD COLUMN {_quote(column)} {_column_type(value)}"
            )
            existing.add(column)
            added.append(column)
    return added


def _write_rows(table: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ToolError("db_write needs at least one row.")
    for entry in rows:
        if not isinstance(entry, dict):
            raise ToolError(
                "Every entry in 'rows' must be an object, e.g. "
                '{"title": "...", "url": "..."}.'
            )

    connection = _connect()
    try:
        added = _ensure_columns(connection, table, rows)
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()

        # One statement, executed once per row, inside a single transaction. The
        # column list is the union across the batch, so a row missing a key is
        # inserted as NULL rather than shifting every later value one place to
        # the left — which is what a per-row column list would do, silently.
        columns: list[str] = []
        for row in rows:
            for key in row:
                column = _check_identifier(key, "column")
                if column not in columns:
                    columns.append(column)

        placeholders = ", ".join("?" for _ in columns)
        names = ", ".join(_quote(column) for column in columns)
        statement = (
            f"INSERT INTO {_quote(table)} "
            f"({names}, {_quote(_INSERTED_AT)}) VALUES ({placeholders}, ?)"
        )
        payload = [
            [_as_stored(row.get(column)) for column in columns] + [stamp]
            for row in rows
        ]
        connection.executemany(statement, payload)
        connection.commit()

        total = connection.execute(
            f"SELECT COUNT(*) AS n FROM {_quote(table)}"
        ).fetchone()["n"]
    finally:
        connection.close()

    return {
        "table": table,
        "inserted": len(rows),
        "rows_in_table": int(total),
        "columns_added": added,
    }


def _update_rows(
    table: str,
    values: dict[str, Any],
    where_column: str,
    where_value: Any,
) -> dict[str, Any]:
    """Update rows matching one equality condition.

    The condition is required: an UPDATE with no WHERE is a mass rewrite and
    exactly the accident this tool should make impossible. At most 500 rows are
    touched per call, and the count says how many were.
    """
    if not isinstance(values, dict) or not values:
        raise ToolError("db_update needs a non-empty 'values' object.")
    if not where_column or where_value is None:
        raise ToolError("db_update requires where_column and where_value.")
    column = _check_identifier(where_column, "column")
    added: list[str] = []
    connection = _connect()
    try:
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            return {
                "table": table,
                "updated": 0,
                "detail": f"Table '{table}' does not exist.",
            }
        added = _ensure_columns(connection, table, [values])
        assignments = ", ".join(
            f"{_quote(_check_identifier(key, 'column'))} = ?" for key in values
        )
        payload = [_as_stored(value) for value in values.values()]
        payload.append(_as_stored(where_value))
        cursor = connection.execute(
            f"UPDATE {_quote(table)} SET {assignments} "
            f"WHERE {_quote(_ROW_ID)} IN ("
            f"SELECT {_quote(_ROW_ID)} FROM {_quote(table)} "
            f"WHERE {_quote(column)} = ? LIMIT 500)",
            payload,
        )
        connection.commit()
        updated = int(cursor.rowcount or 0)
    finally:
        connection.close()
    return {
        "table": table,
        "updated": updated,
        "columns_added": added,
        "limited": updated >= 500,
    }


def _delete_rows(
    table: str,
    where_column: str,
    where_value: Any,
) -> dict[str, Any]:
    """Delete rows matching one equality condition, capped at 500 per call."""
    if not where_column or where_value is None:
        raise ToolError("db_delete requires where_column and where_value.")
    column = _check_identifier(where_column, "column")
    connection = _connect()
    try:
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            return {
                "table": table,
                "deleted": 0,
                "detail": f"Table '{table}' does not exist.",
            }
        cursor = connection.execute(
            f"DELETE FROM {_quote(table)} "
            f"WHERE {_quote(_ROW_ID)} IN ("
            f"SELECT {_quote(_ROW_ID)} FROM {_quote(table)} "
            f"WHERE {_quote(column)} = ? LIMIT 500)",
            (_as_stored(where_value),),
        )
        connection.commit()
        deleted = int(cursor.rowcount or 0)
    finally:
        connection.close()
    return {"table": table, "deleted": deleted, "limited": deleted >= 500}


def _query_rows(
    table: str,
    limit: int = 20,
    where_column: str = "",
    where_value: Any = None,
    newest_first: bool = True,
) -> dict[str, Any]:
    connection = _connect()
    try:
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            return {
                "table": table,
                "rows": [],
                "rows_in_table": 0,
                "detail": f"Table '{table}' does not exist yet.",
            }

        total = connection.execute(
            f"SELECT COUNT(*) AS n FROM {_quote(table)}"
        ).fetchone()["n"]

        clause = ""
        params: list[Any] = []
        if where_column:
            clause = f" WHERE {_quote(_check_identifier(where_column, 'column'))} = ?"
            params.append(_as_stored(where_value))

        order = _quote(_ROW_ID)
        direction = "DESC" if newest_first else "ASC"
        capped = max(1, min(int(limit or 20), 500))
        cursor = connection.execute(
            f"SELECT * FROM {_quote(table)}{clause} "
            f"ORDER BY {order} {direction} LIMIT {capped}",
            params,
        )
        rows = [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()

    return {
        "table": table,
        "rows": _decode(rows),
        "rows_in_table": int(total),
        "returned": len(rows),
    }


def _decode(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse JSON-text columns back into structures.

    A column stored as JSON text is indistinguishable from a column that
    genuinely holds a JSON-looking string. Guessing wrong is the cheaper
    mistake: a string that parses as JSON was almost certainly written by
    ``_as_stored``, and one that does not parse is returned unchanged.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        decoded: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, str) and value[:1] in "[{":
                try:
                    decoded[key] = json.loads(value)
                    continue
                except json.JSONDecodeError:
                    pass
            decoded[key] = value
        out.append(decoded)
    return out


def _list_tables() -> dict[str, Any]:
    connection = _connect()
    try:
        names = [
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables = []
        for name in names:
            count = connection.execute(
                f"SELECT COUNT(*) AS n FROM {_quote(name)}"
            ).fetchone()["n"]
            tables.append({"table": name, "rows": int(count)})
    finally:
        connection.close()
    return {"tables": tables, "total_rows": sum(t["rows"] for t in tables)}


def _reset(table: str = "") -> dict[str, Any]:
    """Drop one table, or every table. For a demo that must start clean.

    Guarded to this file by construction — ``_connect`` only ever opens
    ``results.db`` — but it is still a destructive capability and is registered
    as dangerous so the UI warns before a graph is run.
    """
    connection = _connect()
    try:
        if table:
            connection.execute(f"DROP TABLE IF EXISTS {_quote(table)}")
            dropped = [table]
        else:
            dropped = [
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for name in dropped:
                connection.execute(f"DROP TABLE IF EXISTS {_quote(name)}")
        connection.commit()
    finally:
        connection.close()
    return {"dropped": dropped}


# ---------------------------------------------------------------------------
# Read-only snapshot — what the live panel polls
# ---------------------------------------------------------------------------


def snapshot(preview_rows: int = 8) -> dict[str, Any]:
    """Current contents of the results database, for the canvas's live panel.

    Synchronous, and called through ``asyncio.to_thread`` by the API. Returns
    ``{"exists", "path", "tables": [{name, rows, columns, preview}]}`` — the
    shape the panel renders directly, so the frontend has no schema knowledge of
    its own to drift.
    """
    path = db_path()
    if not path.exists():
        return {"exists": False, "path": str(path), "tables": [], "total_rows": 0}

    connection = _connect()
    try:
        names = [
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables: list[dict[str, Any]] = []
        for name in names:
            count = int(
                connection.execute(
                    f"SELECT COUNT(*) AS n FROM {_quote(name)}"
                ).fetchone()["n"]
            )
            columns = [
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({_quote(name)})")
            ]
            cursor = connection.execute(
                f"SELECT * FROM {_quote(name)} "
                f"ORDER BY {_quote(_ROW_ID)} DESC LIMIT {max(0, int(preview_rows))}"
            )
            preview = _decode([dict(row) for row in cursor.fetchall()])
            tables.append(
                {"name": name, "rows": count, "columns": columns, "preview": preview}
            )
    finally:
        connection.close()

    return {
        "exists": True,
        "path": str(path),
        "tables": tables,
        "total_rows": sum(t["rows"] for t in tables),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def _db_write(table: str, rows: Any) -> dict[str, Any]:
    name = _check_identifier(table, "table")
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except json.JSONDecodeError as exc:
            raise ToolError(f"'rows' is a string but not valid JSON: {exc}") from exc
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        raise ToolError("'rows' must be an object or a list of objects.")
    return await asyncio.to_thread(_write_rows, name, rows)


async def _db_query(
    table: str,
    limit: int = 20,
    where_column: str = "",
    where_value: Any = None,
    newest_first: bool = True,
) -> dict[str, Any]:
    name = _check_identifier(table, "table")
    return await asyncio.to_thread(
        _query_rows, name, limit, where_column, where_value, newest_first
    )


async def _db_update(
    table: str,
    values: Any,
    where_column: str,
    where_value: Any,
) -> dict[str, Any]:
    name = _check_identifier(table, "table")
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except json.JSONDecodeError as exc:
            raise ToolError(f"'values' is a string but not valid JSON: {exc}") from exc
    if not isinstance(values, dict):
        raise ToolError("'values' must be an object of column -> new value.")
    return await asyncio.to_thread(
        _update_rows, name, values, where_column, where_value
    )


async def _db_delete(
    table: str, where_column: str, where_value: Any
) -> dict[str, Any]:
    name = _check_identifier(table, "table")
    return await asyncio.to_thread(
        _delete_rows, name, where_column, where_value
    )


async def _db_tables() -> dict[str, Any]:
    return await asyncio.to_thread(_list_tables)


async def _db_reset(table: str = "") -> dict[str, Any]:
    name = _check_identifier(table, "table") if table else ""
    return await asyncio.to_thread(_reset, name)


register_tool(
    ToolDef(
        name="db_write",
        label="Write to database",
        label_zh="写入数据库",
        description=(
            "Append rows to a table in the results database. The table and any "
            "new columns are created automatically. Use this to record findings "
            "so they outlive the run and can be counted. Pass 'rows' as a list of "
            "objects, e.g. [{\"title\": \"...\", \"url\": \"...\"}]."
        ),
        description_zh=(
            "把若干行追加写进结果数据库的某张表。表和缺失的列会自动创建。"
            "用它把找到的东西记下来，让结果在运行结束后仍然存在、并且可以被统计。"
            "'rows' 传对象数组，例如 [{\"title\": \"...\", \"url\": \"...\"}]。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Table name, e.g. 'findings'. Created if missing.",
                },
                "rows": {
                    "description": (
                        "One object, or a list of objects. Every key becomes a column."
                    ),
                },
            },
            "required": ["table", "rows"],
        },
        fn=_db_write,
        dangerous=True,
    )
)

register_tool(
    ToolDef(
        name="db_query",
        label="Read from database",
        label_zh="读取数据库",
        description=(
            "Read rows back from the results database, newest first, and get the "
            "table's total row count. Use it to check what has been recorded so "
            "far, or to gather rows for a summary."
        ),
        description_zh=(
            "从结果数据库里读回若干行（最新的在前），并返回该表的总行数。"
            "用它查看目前已经记录了哪些内容，或取出行来做汇总。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Table name to read."},
                "limit": {
                    "type": "integer",
                    "description": "How many rows to return (1-500). Default 20.",
                    "default": 20,
                },
                "where_column": {
                    "type": "string",
                    "description": "Optional column to filter on.",
                    "default": "",
                },
                "where_value": {
                    "description": "Value the column must equal. Only used with where_column."
                },
                "newest_first": {
                    "type": "boolean",
                    "description": "Newest rows first. Default true.",
                    "default": True,
                },
            },
            "required": ["table"],
        },
        fn=_db_query,
    )
)

register_tool(
    ToolDef(
        name="db_update",
        label="Update database rows",
        label_zh="更新数据库行",
        description=(
            "Update columns on rows matching one equality condition. "
            "'where_column' and 'where_value' are required, and at most 500 rows "
            "are changed per call. Use this to correct recorded data."
        ),
        description_zh=(
            "按一个等值条件更新若干行的列。必须提供 where_column 与 where_value，"
            "每次最多修改 500 行。用于纠正已记录的数据。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "values": {
                    "type": "object",
                    "description": "Column -> new value.",
                },
                "where_column": {"type": "string"},
                "where_value": {},
            },
            "required": ["table", "values", "where_column", "where_value"],
        },
        fn=_db_update,
        dangerous=True,
    )
)

register_tool(
    ToolDef(
        name="db_delete",
        label="Delete database rows",
        label_zh="删除数据库行",
        description=(
            "Delete rows matching one equality condition. Both 'where_column' "
            "and 'where_value' are required; at most 500 rows are deleted per "
            "call. Destructive."
        ),
        description_zh=(
            "按一个等值条件删除行。必须提供 where_column 与 where_value；每次最多"
            "删除 500 行。破坏性操作。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "where_column": {"type": "string"},
                "where_value": {},
            },
            "required": ["table", "where_column", "where_value"],
        },
        fn=_db_delete,
        dangerous=True,
    )
)

register_tool(
    ToolDef(
        name="db_tables",
        label="List database tables",
        label_zh="列出数据库表",
        description=(
            "List every table in the results database with its row count. Use it "
            "when you do not know what has been recorded yet."
        ),
        description_zh="列出结果数据库里的所有表以及各自的行数。不确定已记录了什么时使用。",
        parameters={"type": "object", "properties": {}},
        fn=_db_tables,
    )
)

register_tool(
    ToolDef(
        name="db_reset",
        label="Clear database tables",
        label_zh="清空数据库表",
        description=(
            "Delete a table, or every table when 'table' is empty. Destructive: "
            "the rows are gone. Only useful when a workflow is meant to start "
            "from a clean slate."
        ),
        description_zh=(
            "删除一张表；'table' 留空则删除所有表。这是破坏性操作，数据会真的消失。"
            "只在希望工作流从空表开始时才有用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Table to drop. Empty drops every table.",
                    "default": "",
                }
            },
        },
        fn=_db_reset,
        dangerous=True,
    )
)
