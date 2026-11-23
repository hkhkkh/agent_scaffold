"""The files capability pack: read and write the agent's own workspace.

What this pack is
-----------------
Ten tools that let a model keep working state on disk: plain text, CSV, Excel,
PDF and Word. Without them a workflow cannot read an uploaded spreadsheet or
hand back a report, and the Code node cannot help -- its sandbox removes
``open`` (see ``app/runtime/sandbox.py``), so a Tool is the only seam.

The workspace is the boundary
-----------------------------
A model will happily emit ``../../.env`` or ``C:\\Users\\...``. The tools never
interpret a raw path: every name is resolved by
:class:`app.runtime.workspace.Workspace`, which refuses absolute paths, ``..``
components, NUL bytes, Windows device names and symlinks that resolve outside
the root. The same object bounds bytes per read and per write, so a 2 GB
upload gets a clear refusal instead of eating the disk. Every error is turned
into a ``ToolError`` that says what was wrong and, for a missing file, lists
what does exist so the model can correct itself.

Lazy third-party imports
------------------------
``openpyxl``, ``pypdf`` and ``python-docx`` are imported *inside* the function
that needs them. Importing this module therefore does not require any of them:
the platform starts on a machine that has none, and the pack registry's doctor
report names what is missing. A missing package becomes a ``ToolError`` naming
the pip distribution to install -- never an ImportError from inside a run.

Writes are checked, recorded, then performed
--------------------------------------------
A mutation checks the ``files.write`` permission first, records the intent in
the audit trail second, and only then touches the file. If the audit recorder
cannot write, the mutation fails closed: an unrecorded write is the one outcome
this layer must not allow. ``Workspace.write_bytes`` is additionally atomic
(temp file plus ``os.replace``), so a reader never sees half a spreadsheet.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
from pathlib import Path
from typing import Any

from app.runtime.audit import record_audit
from app.runtime.packs.manifest import PackDependency, PackManifest
from app.runtime.packs.registry import CapabilityPack
from app.runtime.permissions import (
    FILES_LIST,
    FILES_READ,
    FILES_WRITE,
    check_permission,
)
from app.runtime.workspace import Workspace, WorkspaceError, workspace_from_env
from app.tools.registry import ToolDef, ToolError

__all__ = [
    "FILES_MANIFEST",
    "FILES_PACK",
    "PACK_MANIFEST",
    "TOOL_NAMES",
    "tools",
]

#: Tool names in the order the manifest declares them, and the palette shows
#: them. Related tools are kept together so the Agent node's tool list reads as
#: file -> table -> document rather than alphabetically.
TOOL_NAMES: tuple[str, ...] = (
    "file_list",
    "file_read",
    "file_write",
    "csv_read",
    "csv_write",
    "excel_read",
    "excel_write",
    "pdf_read",
    "docx_read",
    "docx_write",
)

#: Tools that change the workspace. They are the ones flagged ``dangerous`` for
#: the UI warning, and the only ones that need the audit-before-write ordering.
WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {"file_write", "csv_write", "excel_write", "docx_write"}
)

PACK_MANIFEST = PackManifest(
    name="files",
    version="1.0.0",
    title="Workspace files",
    title_zh="工作区文件",
    description=(
        "Read and write files in the agent's own workspace: plain text, CSV, "
        "Excel workbooks, PDFs and Word documents. Every path is confined to "
        "the workspace; writes are permission-checked and audited before they "
        "happen."
    ),
    description_zh=(
        "在 agent 自己的工作区内读写文件：纯文本、CSV、Excel、PDF 与 Word。"
        "所有路径都限制在工作区内，写操作先经过权限校验并记入审计。"
    ),
    tools=TOOL_NAMES,
    permissions=(FILES_LIST, FILES_READ, FILES_WRITE),
    dependencies=(
        PackDependency(spec="openpyxl>=3.1", import_name="openpyxl"),
        PackDependency(spec="python-docx>=1.1", import_name="docx"),
        PackDependency(spec="pypdf>=5", import_name="pypdf"),
    ),
    notes=(
        "Files live under the workspace root (AGENT_WORKSPACE_DIR, else "
        "<DATA_DIR>/workspaces/<id>). Absolute paths, '..', NUL bytes, Windows "
        "device names and symlink escapes are refused; Workspace enforces "
        "per-file read/write byte caps. Document libraries are optional and "
        "imported lazily, so importing this pack never requires them."
    ),
)

#: The placeholder version of this module named the manifest FILES_MANIFEST.
#: Kept as an alias so early wiring (and readers following the old name) keeps
#: working; PACK_MANIFEST is the canonical name.
FILES_MANIFEST = PACK_MANIFEST


# ---------------------------------------------------------------------------
# Shared plumbing: workspace, paths, permissions, audit
# ---------------------------------------------------------------------------


def _workspace() -> Workspace:
    """The workspace for this call.

    Resolved from the environment on every call, not cached: a test, a canvas
    run and an exported agent switch roots by setting ``AGENT_WORKSPACE_DIR``,
    and a cached root would let the first caller win for the whole process.
    """
    return workspace_from_env()


def _call_error(tool: str, message: str) -> ToolError:
    """A tool failure whose message starts with the tool name.

    A model reading a tool result needs to know which call failed when several
    ran in one step; the prefix costs nothing and makes that unambiguous.
    """
    return ToolError(f"{tool}: {message}")


def _audit(
    action: str,
    *,
    tool: str,
    target: str,
    detail: dict[str, Any] | None = None,
    outcome: str = "ok",
) -> None:
    """Record one action.

    Deliberately does not swallow: a recorder that cannot append raises
    :class:`app.runtime.audit.AuditError`, and the caller relies on that to fail
    closed instead of performing an unrecorded mutation.
    """
    record_audit(
        action,
        outcome=outcome,
        tool=tool,
        target=target,
        detail=dict(detail or {}),
    )


def _resolve(workspace: Workspace, path: Any, *, tool: str) -> tuple[str, Path]:
    """Validate one caller-supplied name. Returns ``(relative, resolved)``.

    Raises ``ToolError``, never ``WorkspaceError``: a tool result is read by a
    model, and a model can act on "absolute paths are refused" but not on a
    traceback. Absolute paths, ``..``, NUL bytes, Windows device names and
    symlink escapes are all rejected by ``Workspace.resolve``; this function
    only translates the refusal.
    """
    raw = "" if path is None else str(path).strip()
    if not raw:
        raise _call_error(tool, "'path' is required (a file name relative to the workspace)")
    try:
        resolved = workspace.resolve(raw)
        relative = workspace.relative(resolved)
    except WorkspaceError as exc:
        raise _call_error(tool, str(exc)) from exc
    return relative, resolved


def _workspace_files_hint(workspace: Workspace) -> str:
    """What does exist, so a model can correct a wrong file name.

    Capped at ten names: the hint is a nudge, not a directory dump. A workspace
    with thousands of files would otherwise put all of them in the tool result.
    """
    try:
        names = [entry["name"] for entry in workspace.list(recursive=True)][:10]
    except (OSError, ValueError):
        return ""
    if not names:
        return " The workspace is empty."
    return " Files in the workspace: " + ", ".join(names) + "."


def _require_file(
    workspace: Workspace, relative: str, resolved: Path, *, tool: str
) -> None:
    if not resolved.is_file():
        raise _call_error(
            tool,
            f"file '{relative}' does not exist in the workspace."
            + _workspace_files_hint(workspace),
        )


def _check_read(
    workspace: Workspace,
    relative: str,
    resolved: Path,
    *,
    tool: str,
    permission: str = FILES_READ,
) -> None:
    """Permission first, then existence: a policy refusal must not be hidden."""
    check_permission(permission, tool=tool, context={"path": relative})
    _require_file(workspace, relative, resolved, tool=tool)


def _read_bytes(
    workspace: Workspace,
    relative: str,
    resolved: Path,
    *,
    tool: str,
    limit: int | None = None,
) -> bytes:
    try:
        return workspace.read_bytes(relative, limit=limit)
    except WorkspaceError as exc:
        raise _call_error(tool, str(exc)) from exc


def _read_text(
    workspace: Workspace,
    relative: str,
    resolved: Path,
    *,
    tool: str,
    limit: int | None = None,
) -> str:
    try:
        return workspace.read_text(relative, limit=limit)
    except WorkspaceError as exc:
        raise _call_error(tool, str(exc)) from exc


def _write_bytes(
    workspace: Workspace,
    relative: str,
    data: bytes,
    *,
    tool: str,
    action: str,
    detail: dict[str, Any] | None = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Check, audit, then write -- in that order.

    The audit entry is written *before* the file on purpose. If the recorder
    cannot append, this raises ``AuditError`` and the file is never touched:
    that is the failure-closed behaviour the audit module documents, and the
    only way "the agent wrote this" and "the trail says so" cannot diverge.
    """
    check_permission(FILES_WRITE, tool=tool, context={"path": relative})
    if len(data) > workspace.max_write_bytes:
        raise _call_error(
            tool,
            f"refusing to write {len(data)} bytes; this deployment allows at "
            f"most {workspace.max_write_bytes} bytes per file",
        )
    _audit(
        action,
        tool=tool,
        target=relative,
        detail={"size": len(data), **dict(detail or {})},
    )
    try:
        return workspace.write_bytes(relative, data, overwrite=overwrite)
    except WorkspaceError as exc:
        raise _call_error(tool, str(exc)) from exc


def _int_param(
    value: Any, name: str, *, default: int, minimum: int, tool: str
) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise _call_error(tool, f"'{name}' must be an integer, got {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise _call_error(tool, f"'{name}' must be an integer, got {value!r}") from exc
    if number < minimum:
        raise _call_error(tool, f"'{name}' must be at least {minimum}, got {number}")
    return number


def _bool_param(value: Any, name: str, *, default: bool, tool: str) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y", "on"):
            return True
        if lowered in ("false", "0", "no", "n", "off"):
            return False
    raise _call_error(tool, f"'{name}' must be true or false, got {value!r}")


# ---------------------------------------------------------------------------
# Lazy third-party imports. The pack must import without any of these.
# ---------------------------------------------------------------------------


def _load_openpyxl():
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - deployment dependent
        raise ToolError(
            "excel support needs the 'openpyxl' package; install it with "
            "pip install 'openpyxl>=3.1'"
        ) from exc
    return openpyxl


def _load_docx():
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - deployment dependent
        raise ToolError(
            "Word (.docx) support needs the 'python-docx' package; install it "
            "with pip install 'python-docx>=1.1'"
        ) from exc
    return docx


def _load_pdf_reader():
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - deployment dependent
        raise ToolError(
            "PDF support needs the 'pypdf' package; install it with "
            "pip install 'pypdf>=5'"
        ) from exc
    return PdfReader


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------


def _text_value(value: Any) -> str:
    """One cell as CSV text. ``None`` is empty; booleans read as true/false."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return str(value)


def _jsonable_value(value: Any) -> Any:
    """Turn a spreadsheet cell into something JSON can carry."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    return str(value)


def _excel_value(value: Any) -> Any:
    """A JSON value as an openpyxl cell: keep native types, stringify the rest."""
    if value is None or isinstance(
        value,
        (str, bool, int, float, dt.datetime, dt.date, dt.time, dt.timedelta),
    ):
        return value
    return str(value)


def _header_columns(values: Any) -> list[str]:
    """Column names from a header row.

    Blank cells become ``column_N`` and duplicates get a numeric suffix, so the
    row dictionaries a model receives are always addressable.
    """
    if values is None:
        return []
    columns: list[str] = []
    counts: dict[str, int] = {}
    for index, value in enumerate(values):
        text = "" if value is None else str(value).strip()
        if not text:
            text = f"column_{index + 1}"
        counts[text] = counts.get(text, 0) + 1
        if counts[text] > 1:
            text = f"{text}_{counts[text]}"
        columns.append(text)
    return columns


# ---------------------------------------------------------------------------
# file_list
# ---------------------------------------------------------------------------


def _list_glob(workspace: Workspace, directory: Any, pattern: Any) -> str:
    """Validate the optional directory and pattern, and combine them.

    ``Workspace.list`` takes a glob relative to the root, so the directory is
    resolved first (which enforces containment) and the pattern is appended.
    The pattern is validated separately because a glob is *meant* to contain
    wildcards: it gets its own "no absolute path, no '..'" check rather than the
    per-component rules ``Workspace.resolve`` applies to plain names.
    """
    directory_text = str(directory or "").strip()
    pattern_text = str(pattern or "").strip() or "*"
    pattern_text = pattern_text.replace("\\", "/")
    if "\x00" in pattern_text:
        raise _call_error("file_list", "'pattern' contains a NUL byte")
    pattern_path = Path(pattern_text)
    if (
        pattern_text.startswith(("/", "\\"))
        or pattern_path.is_absolute()
        or pattern_path.drive
    ):
        raise _call_error(
            "file_list",
            "'pattern' must be relative to the workspace, not an absolute path",
        )
    if any(part == ".." for part in pattern_text.split("/")):
        raise _call_error("file_list", "'pattern' may not contain '..'")
    prefix = ""
    if directory_text:
        relative_dir, resolved_dir = _resolve(workspace, directory_text, tool="file_list")
        if not resolved_dir.is_dir():
            raise _call_error(
                "file_list",
                f"'{relative_dir}' is not a directory in the workspace."
                + _workspace_files_hint(workspace),
            )
        prefix = "" if relative_dir in ("", ".") else relative_dir
    return f"{prefix}/{pattern_text}" if prefix else pattern_text


async def file_list(directory: Any = "", pattern: Any = "*") -> dict[str, Any]:
    """List files, optionally inside a sub-directory and matching a glob."""
    workspace = _workspace()
    glob = _list_glob(workspace, directory, pattern)
    check_permission(FILES_LIST, tool="file_list", context={"pattern": glob})
    try:
        # Recursive so a file written as 'reports/q1.csv' is still visible from
        # the root listing; otherwise "list the workspace" silently hides
        # everything a model put in a sub-directory.
        entries = workspace.list(glob, recursive=True)
    except (OSError, ValueError) as exc:
        raise _call_error("file_list", f"cannot list '{glob}': {exc}") from exc
    _audit("file.list", tool="file_list", target=glob, detail={"count": len(entries)})
    return {"files": entries}


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------


async def file_read(path: Any, max_bytes: Any = None) -> dict[str, Any]:
    """Read one text file (UTF-8, falling back to GB18030)."""
    workspace = _workspace()
    relative, resolved = _resolve(workspace, path, tool="file_read")
    limit = _int_param(
        max_bytes,
        "max_bytes",
        default=workspace.max_read_bytes,
        minimum=1,
        tool="file_read",
    )
    _check_read(workspace, relative, resolved, tool="file_read")
    content = _read_text(workspace, relative, resolved, tool="file_read", limit=limit)
    _audit("file.read", tool="file_read", target=relative, detail={"max_bytes": limit})
    return {"name": relative, "content": content}


async def file_write(
    path: Any, content: Any, overwrite: Any = True
) -> dict[str, Any]:
    """Write one UTF-8 text file, creating parent directories as needed."""
    workspace = _workspace()
    relative, _resolved = _resolve(workspace, path, tool="file_write")
    if content is None:
        raise _call_error("file_write", "'content' is required")
    text = content if isinstance(content, str) else str(content)
    overwrite_flag = _bool_param(overwrite, "overwrite", default=True, tool="file_write")
    return _write_bytes(
        workspace,
        relative,
        text.encode("utf-8"),
        tool="file_write",
        action="file.write",
        detail={"overwrite": overwrite_flag},
        overwrite=overwrite_flag,
    )


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


async def csv_read(
    path: Any, delimiter: Any = ",", limit: Any = 200
) -> dict[str, Any]:
    """Read a CSV file as a header plus rows."""
    workspace = _workspace()
    relative, resolved = _resolve(workspace, path, tool="csv_read")
    row_limit = _int_param(limit, "limit", default=200, minimum=0, tool="csv_read")
    separator = "," if delimiter in (None, "") else str(delimiter)
    if len(separator) != 1:
        raise _call_error(
            "csv_read",
            f"'delimiter' must be a single character, got {delimiter!r}",
        )
    _check_read(workspace, relative, resolved, tool="csv_read")
    text = _read_text(workspace, relative, resolved, tool="csv_read")
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=separator))
    except csv.Error as exc:
        raise _call_error(
            "csv_read", f"'{relative}' is not readable as CSV: {exc}"
        ) from exc
    columns: list[str] = []
    data_rows = rows
    if rows:
        columns = [str(cell) for cell in rows[0]]
        data_rows = rows[1:]
    total_rows = len(data_rows)
    selected = [list(row) for row in data_rows[:row_limit]]
    _audit(
        "file.csv_read",
        tool="csv_read",
        target=relative,
        detail={"rows": total_rows, "columns": len(columns)},
    )
    return {
        "columns": columns,
        "rows": selected,
        "total_rows": total_rows,
        "truncated": total_rows > len(selected),
    }


async def csv_write(
    path: Any, rows: Any, columns: Any = None
) -> dict[str, Any]:
    """Write rows (and an optional header) as UTF-8 CSV with CRLF endings."""
    workspace = _workspace()
    relative, _resolved = _resolve(workspace, path, tool="csv_write")
    if rows is None or isinstance(rows, (str, bytes)) or not isinstance(
        rows, (list, tuple)
    ):
        raise _call_error("csv_write", "'rows' is required and must be a list of rows")
    if columns in (None, "", []):
        header: list[Any] | None = None
    elif isinstance(columns, (list, tuple)):
        header = list(columns)
    else:
        raise _call_error("csv_write", "'columns' must be a list of column names")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    if header is not None:
        writer.writerow([_text_value(value) for value in header])
    for index, row in enumerate(rows):
        if isinstance(row, (str, bytes)) or not isinstance(row, (list, tuple)):
            raise _call_error(
                "csv_write",
                f"row {index + 1} must be a list of values, got {type(row).__name__}",
            )
        writer.writerow([_text_value(value) for value in row])
    return _write_bytes(
        workspace,
        relative,
        buffer.getvalue().encode("utf-8"),
        tool="csv_write",
        action="file.csv_write",
        detail={"rows": len(rows), "columns": len(header or ())},
    )


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def _excel_payload(
    sheets: Any, rows: Any, sheet_name: Any
) -> dict[str, list[Any]]:
    if sheets is not None and rows is not None:
        raise _call_error("excel_write", "pass either 'sheets' or 'rows', not both")
    payload: Any = sheets
    if payload is None:
        if rows is None:
            raise _call_error("excel_write", "'sheets' or 'rows' is required")
        payload = {str(sheet_name or "Sheet1"): rows}
    if not isinstance(payload, dict) or not payload:
        raise _call_error(
            "excel_write",
            "'sheets' must be a non-empty object mapping sheet name to a list of rows",
        )
    cleaned: dict[str, list[Any]] = {}
    for name, data in payload.items():
        title = str(name or "").strip() or "Sheet1"
        if not isinstance(data, (list, tuple)):
            raise _call_error(
                "excel_write", f"sheet '{title}' must be a list of rows"
            )
        cleaned[title] = list(data)
    return cleaned


def _fill_sheet(worksheet: Any, data: list[Any]) -> None:
    """Fill one worksheet from either a list of objects or a list of rows."""
    if not data:
        return
    first = data[0]
    if isinstance(first, dict):
        records: list[dict[str, Any]] = []
        for record in data:
            if not isinstance(record, dict):
                raise _call_error(
                    "excel_write",
                    "rows must be either all objects (with named keys) or all lists",
                )
            records.append({str(key): value for key, value in record.items()})
        columns: list[str] = []
        for record in records:
            for key in record:
                if key not in columns:
                    columns.append(key)
        worksheet.append(columns)
        for record in records:
            worksheet.append([_excel_value(record.get(column)) for column in columns])
        return
    if isinstance(first, (list, tuple)):
        for index, row in enumerate(data):
            if isinstance(row, (str, bytes)) or not isinstance(row, (list, tuple)):
                raise _call_error(
                    "excel_write",
                    f"row {index + 1} must be a list of cell values",
                )
            worksheet.append([_excel_value(value) for value in row])
        return
    raise _call_error(
        "excel_write",
        "each sheet's rows must be objects with named keys, or lists of cell values",
    )


async def excel_read(
    path: Any, sheet: Any = None, header_row: Any = 1, limit: Any = 200
) -> dict[str, Any]:
    """Read one worksheet as columns plus row dictionaries."""
    workspace = _workspace()
    relative, resolved = _resolve(workspace, path, tool="excel_read")
    header_index = _int_param(
        header_row, "header_row", default=1, minimum=1, tool="excel_read"
    )
    row_limit = _int_param(limit, "limit", default=200, minimum=0, tool="excel_read")
    _check_read(workspace, relative, resolved, tool="excel_read")
    openpyxl = _load_openpyxl()
    raw = _read_bytes(workspace, relative, resolved, tool="excel_read")
    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(raw), read_only=True, data_only=True
        )
    except Exception as exc:  # noqa: BLE001 - any parse failure is a bad input
        raise _call_error(
            "excel_read",
            f"'{relative}' is not a readable .xlsx workbook "
            f"({type(exc).__name__}: {exc})",
        ) from exc
    try:
        sheet_names = list(workbook.sheetnames)
        wanted = "" if sheet is None else str(sheet).strip()
        if wanted:
            if wanted not in sheet_names:
                raise _call_error(
                    "excel_read",
                    f"sheet '{wanted}' does not exist in '{relative}'. "
                    f"Available sheets: {', '.join(sheet_names)}",
                )
            worksheet = workbook[wanted]
        else:
            worksheet = workbook.active
            if worksheet is None:
                raise _call_error(
                    "excel_read", f"'{relative}' contains no worksheets"
                )
            wanted = worksheet.title
        collected: list[tuple[Any, ...]] = []
        total_rows = 0
        header_values: Any = None
        for row_number, values in enumerate(
            worksheet.iter_rows(values_only=True), start=1
        ):
            if row_number < header_index:
                continue
            if row_number == header_index:
                header_values = values
                continue
            total_rows += 1
            if total_rows <= row_limit:
                collected.append(tuple(values))
        columns = _header_columns(header_values)
        rows: list[dict[str, Any]] = []
        for values in collected:
            record: dict[str, Any] = {}
            for index, column in enumerate(columns):
                value = values[index] if index < len(values) else None
                record[column] = _jsonable_value(value)
            rows.append(record)
    finally:
        try:
            workbook.close()
        except Exception:  # noqa: BLE001 - closing a read-only workbook is best effort
            pass
    _audit(
        "file.excel_read",
        tool="excel_read",
        target=relative,
        detail={"sheet": wanted, "rows": total_rows},
    )
    return {
        "sheet": wanted,
        "sheets": sheet_names,
        "columns": columns,
        "rows": rows,
        "total_rows": total_rows,
        "truncated": total_rows > len(rows),
    }


async def excel_write(
    path: Any,
    sheets: Any = None,
    rows: Any = None,
    sheet_name: Any = "Sheet1",
) -> dict[str, Any]:
    """Write an .xlsx workbook. Either many sheets or one ``rows`` table."""
    workspace = _workspace()
    relative, _resolved = _resolve(workspace, path, tool="excel_write")
    payload = _excel_payload(sheets, rows, sheet_name)
    openpyxl = _load_openpyxl()
    try:
        workbook = openpyxl.Workbook()
        default_sheet = workbook.active
        for index, (title, data) in enumerate(payload.items()):
            if index == 0:
                worksheet = default_sheet
                worksheet.title = title
            else:
                worksheet = workbook.create_sheet(title=title)
            _fill_sheet(worksheet, data)
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - any build failure is the caller's data
        raise _call_error(
            "excel_write",
            f"could not build the workbook ({type(exc).__name__}: {exc})",
        ) from exc
    result = _write_bytes(
        workspace,
        relative,
        buffer.getvalue(),
        tool="excel_write",
        action="file.excel_write",
        detail={"sheets": list(payload)},
    )
    return {**result, "sheets": list(payload)}


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


async def pdf_read(path: Any, max_pages: Any = 20) -> dict[str, Any]:
    """Extract embedded text from a PDF, page by page."""
    workspace = _workspace()
    relative, resolved = _resolve(workspace, path, tool="pdf_read")
    page_limit = _int_param(
        max_pages, "max_pages", default=20, minimum=1, tool="pdf_read"
    )
    _check_read(workspace, relative, resolved, tool="pdf_read")
    PdfReader = _load_pdf_reader()
    raw = _read_bytes(workspace, relative, resolved, tool="pdf_read")
    try:
        reader = PdfReader(io.BytesIO(raw))
        if getattr(reader, "is_encrypted", False):
            try:
                decrypted = reader.decrypt("")
            except Exception:  # noqa: BLE001 - an undecryptable file is just refused
                decrypted = 0
            if not decrypted:
                raise _call_error(
                    "pdf_read",
                    f"'{relative}' is encrypted; password-protected PDFs are "
                    "not supported by this tool",
                )
        total_pages = len(reader.pages)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - any parse failure is a bad input
        raise _call_error(
            "pdf_read",
            f"'{relative}' is not a readable PDF "
            f"({type(exc).__name__}: {exc})",
        ) from exc
    pages: list[dict[str, Any]] = []
    for index in range(min(page_limit, total_pages)):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:  # noqa: BLE001 - a damaged page must not kill the document
            text = ""
        pages.append({"page": index + 1, "text": text})
    _audit(
        "file.pdf_read",
        tool="pdf_read",
        target=relative,
        detail={"pages_read": len(pages), "total_pages": total_pages},
    )
    return {"pages": pages, "total_pages": total_pages}


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------


def _docx_paragraph_items(items: Any) -> list[tuple[str, str | None]]:
    if not isinstance(items, (list, tuple)):
        raise _call_error(
            "docx_write", "'paragraphs' must be a list of paragraphs"
        )
    specs: list[tuple[str, str | None]] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            specs.append((item, None))
            continue
        if isinstance(item, dict):
            text = item.get("text")
            if text is None:
                raise _call_error(
                    "docx_write",
                    f"paragraph {index + 1} is an object but has no 'text' field",
                )
            style = item.get("style")
            specs.append((str(text), str(style) if style else None))
            continue
        raise _call_error(
            "docx_write",
            f"paragraph {index + 1} must be a string or an object with 'text'",
        )
    return specs


def _docx_paragraph_specs(payload: Any) -> list[tuple[str, str | None]]:
    """Accept a list of paragraphs, a single string, or a titled document."""
    if isinstance(payload, dict):
        title = payload.get("title")
        body = payload.get("paragraphs")
        if title is None and body is None:
            raise _call_error(
                "docx_write",
                "'paragraphs' must be a list of strings, or an object with "
                "'title' and/or 'paragraphs'",
            )
        specs: list[tuple[str, str | None]] = []
        if title not in (None, ""):
            specs.append((str(title), "Title"))
        if body is not None:
            specs.extend(_docx_paragraph_items(body))
        return specs
    if isinstance(payload, str):
        return [(payload, None)]
    return _docx_paragraph_items(payload)


async def docx_read(
    path: Any, max_paragraphs: Any = 500
) -> dict[str, Any]:
    """Read the paragraphs of a .docx file."""
    workspace = _workspace()
    relative, resolved = _resolve(workspace, path, tool="docx_read")
    paragraph_limit = _int_param(
        max_paragraphs,
        "max_paragraphs",
        default=500,
        minimum=0,
        tool="docx_read",
    )
    _check_read(workspace, relative, resolved, tool="docx_read")
    docx = _load_docx()
    raw = _read_bytes(workspace, relative, resolved, tool="docx_read")
    try:
        document = docx.Document(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001 - any parse failure is a bad input
        raise _call_error(
            "docx_read",
            f"'{relative}' is not a readable .docx file "
            f"({type(exc).__name__}: {exc})",
        ) from exc
    paragraphs: list[str] = []
    for paragraph in document.paragraphs:
        if len(paragraphs) >= paragraph_limit:
            break
        paragraphs.append(paragraph.text)
    _audit(
        "file.docx_read",
        tool="docx_read",
        target=relative,
        detail={"paragraphs": len(paragraphs)},
    )
    return {"paragraphs": paragraphs, "text": "\n".join(paragraphs)}


async def docx_write(path: Any, paragraphs: Any) -> dict[str, Any]:
    """Write a .docx file from a list of paragraphs (or a titled object)."""
    workspace = _workspace()
    relative, _resolved = _resolve(workspace, path, tool="docx_write")
    specs = _docx_paragraph_specs(paragraphs)
    docx = _load_docx()
    try:
        document = docx.Document()
        for text, style in specs:
            if style is None:
                document.add_paragraph(text)
                continue
            try:
                document.add_paragraph(text, style=style)
            except KeyError as exc:
                raise _call_error(
                    "docx_write", f"unknown paragraph style {style!r}"
                ) from exc
        buffer = io.BytesIO()
        document.save(buffer)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - any build failure is the caller's data
        raise _call_error(
            "docx_write",
            f"could not build the document ({type(exc).__name__}: {exc})",
        ) from exc
    result = _write_bytes(
        workspace,
        relative,
        buffer.getvalue(),
        tool="docx_write",
        action="file.docx_write",
        detail={"paragraphs": len(specs)},
    )
    return {**result, "paragraphs": len(specs)}


# ---------------------------------------------------------------------------
# The pack
# ---------------------------------------------------------------------------


def tools() -> list[ToolDef]:
    """Build the pack's tool list. Called once, lazily, by the pack registry."""
    return [
        ToolDef(
            name="file_list",
            label="List files",
            label_zh="列出文件",
            description=(
                "List files in the agent's workspace. Returns each file's name "
                "(relative to the workspace), size in bytes and modification "
                "time. Optionally narrow to a sub-directory and a glob pattern."
            ),
            description_zh=(
                "列出 agent 工作区中的文件，返回文件名（相对工作区）、字节数和"
                "修改时间，可按子目录和通配符筛选。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": (
                            "Sub-directory to list, relative to the workspace, "
                            "e.g. 'reports'. Omit to list the workspace root."
                        ),
                    },
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Glob pattern for file names, e.g. '*.csv'. "
                            "Defaults to '*'. May cross sub-directories with "
                            "'/', but must not contain '..'."
                        ),
                    },
                },
            },
            fn=file_list,
        ),
        ToolDef(
            name="file_read",
            label="Read file",
            label_zh="读取文件",
            description=(
                "Read a text file from the workspace. UTF-8 is tried first, "
                "then GB18030, so Chinese text in either encoding reads "
                "correctly. Returns the file name and its full content."
            ),
            description_zh=(
                "读取工作区中的文本文件。先按 UTF-8 解码，失败后回退 GB18030，"
                "中文内容可正常读出。返回文件名与完整内容。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "File name relative to the workspace, e.g. "
                            "'notes.txt' or 'reports/q1.csv'."
                        ),
                    },
                    "max_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Refuse the read if the file is larger than this "
                            "many bytes. Defaults to the deployment's read cap."
                        ),
                    },
                },
                "required": ["path"],
            },
            fn=file_read,
        ),
        ToolDef(
            name="file_write",
            label="Write file",
            label_zh="写入文件",
            description=(
                "Write a UTF-8 text file in the workspace, creating parent "
                "directories as needed. Returns the file name and the number of "
                "bytes written. The write is audited before it happens."
            ),
            description_zh=(
                "在工作区写入 UTF-8 文本文件，自动创建父目录，返回文件名和写入字节数。"
                "写入前先记入审计。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "File name relative to the workspace, e.g. "
                            "'notes.txt' or 'reports/q1.csv'."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "The UTF-8 text to write.",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": (
                            "Replace an existing file. Defaults to true; set "
                            "false to fail instead of replacing it."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
            fn=file_write,
            dangerous=True,
        ),
        ToolDef(
            name="csv_read",
            label="Read CSV",
            label_zh="读取 CSV",
            description=(
                "Read a CSV file from the workspace. The first row is the "
                "column names; later rows are returned as lists. total_rows "
                "reports the whole file even when limit truncates the rows."
            ),
            description_zh=(
                "读取工作区的 CSV 文件。第一行作为列名，其余行按列表返回。"
                "即使 limit 截断，total_rows 仍报告整个文件的行数。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "CSV file name relative to the workspace.",
                    },
                    "delimiter": {
                        "type": "string",
                        "description": (
                            "Field separator. Defaults to ','; use ';' or a tab "
                            "for other CSV dialects."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Maximum number of data rows to return. Defaults to "
                            "200."
                        ),
                    },
                },
                "required": ["path"],
            },
            fn=csv_read,
        ),
        ToolDef(
            name="csv_write",
            label="Write CSV",
            label_zh="写入 CSV",
            description=(
                "Write rows as a UTF-8 CSV file with CRLF line endings. Pass "
                "'columns' to write a header row first. The write is audited "
                "before it happens."
            ),
            description_zh=(
                "把行数据写成 UTF-8、CRLF 换行的 CSV 文件。传 'columns' 会先写表头。"
                "写入前先记入审计。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "CSV file name relative to the workspace.",
                    },
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {},
                        },
                        "description": (
                            "Rows of values; each row is a list. Values are "
                            "written as text."
                        ),
                    },
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional header row written before the data.",
                    },
                },
                "required": ["path", "rows"],
            },
            fn=csv_write,
            dangerous=True,
        ),
        ToolDef(
            name="excel_read",
            label="Read Excel",
            label_zh="读取 Excel",
            description=(
                "Read one worksheet from a .xlsx workbook. Returns the sheet "
                "name, all sheet names, the column names and row objects. "
                "header_row picks which row holds the column names; limit "
                "truncates rows while total_rows counts the sheet."
            ),
            description_zh=(
                "读取 .xlsx 工作簿中的某个工作表，返回表名、全部表名、列名和行对象。"
                "header_row 指定表头所在行，limit 截断返回行数，total_rows 仍报告整表行数。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workbook file name relative to the workspace.",
                    },
                    "sheet": {
                        "type": "string",
                        "description": (
                            "Worksheet name. Omit to read the workbook's active "
                            "sheet."
                        ),
                    },
                    "header_row": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "1-based row number that holds the column names. "
                            "Defaults to 1."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Maximum number of data rows to return. Defaults to "
                            "200."
                        ),
                    },
                },
                "required": ["path"],
            },
            fn=excel_read,
        ),
        ToolDef(
            name="excel_write",
            label="Write Excel",
            label_zh="写入 Excel",
            description=(
                "Write a .xlsx workbook. 'sheets' maps each worksheet name to "
                "its rows; a row is either an object (its keys become a header "
                "row) or a list of cell values. 'rows' is shorthand for a "
                "single worksheet named by sheet_name. The write is audited "
                "before it happens."
            ),
            description_zh=(
                "写 .xlsx 工作簿。'sheets' 把表名映射到行列表；每行可以是对象"
                "（键名成为表头）或值列表。'rows' 是单表简写，表名由 sheet_name 指定。"
                "写入前先记入审计。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workbook file name relative to the workspace.",
                    },
                    "sheets": {
                        "type": "object",
                        "description": (
                            "Object mapping worksheet name to a list of rows. "
                            "Each row is either an object with named keys or a "
                            "list of cell values."
                        ),
                        "additionalProperties": {
                            "type": "array",
                            "items": {},
                        },
                    },
                    "rows": {
                        "type": "array",
                        "items": {},
                        "description": (
                            "Shorthand for one worksheet: the rows to write to "
                            "sheet_name. Pass either 'sheets' or 'rows'."
                        ),
                    },
                    "sheet_name": {
                        "type": "string",
                        "description": (
                            "Worksheet name used with 'rows'. Defaults to "
                            "'Sheet1'."
                        ),
                    },
                },
                "required": ["path"],
            },
            fn=excel_write,
            dangerous=True,
        ),
        ToolDef(
            name="pdf_read",
            label="Read PDF",
            label_zh="读取 PDF",
            description=(
                "Extract the embedded text of a PDF page by page. Scanned, "
                "image-only PDFs return little or no text; OCR is not available. "
                "total_pages reports the document length even when max_pages "
                "truncates the result."
            ),
            description_zh=(
                "逐页提取 PDF 中的文字。扫描件或纯图片 PDF 几乎取不到文字（当前无 OCR）。"
                "即使 max_pages 截断，total_pages 仍报告总页数。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "PDF file name relative to the workspace.",
                    },
                    "max_pages": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Maximum number of pages to extract. Defaults to 20."
                        ),
                    },
                },
                "required": ["path"],
            },
            fn=pdf_read,
        ),
        ToolDef(
            name="docx_read",
            label="Read Word",
            label_zh="读取 Word",
            description=(
                "Read the paragraphs of a .docx file. Returns the paragraph "
                "texts and their concatenation. max_paragraphs caps how many "
                "are returned."
            ),
            description_zh=(
                "读取 .docx 文件的段落，返回段落文本列表及拼接后的全文。"
                "max_paragraphs 限制返回的段落数。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Word file name relative to the workspace.",
                    },
                    "max_paragraphs": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Maximum number of paragraphs to return. Defaults to "
                            "500."
                        ),
                    },
                },
                "required": ["path"],
            },
            fn=docx_read,
        ),
        ToolDef(
            name="docx_write",
            label="Write Word",
            label_zh="写入 Word",
            description=(
                "Write a .docx file from a list of paragraphs. 'paragraphs' is "
                "normally a list of strings; an object like "
                '{"title": "...", "paragraphs": ["..."]} writes the title first, '
                "and a paragraph may be written as "
                '{"text": "...", "style": "Heading 1"}. The write is audited '
                "before it happens."
            ),
            description_zh=(
                "把段落列表写成 .docx 文件。'paragraphs' 通常是字符串列表；"
                '也可传 {"title": "...", "paragraphs": [...]} 先写标题，'
                '段落也可写成 {"text": "...", "style": "Heading 1"}。'
                "写入前先记入审计。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Word file name relative to the workspace.",
                    },
                    "paragraphs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Paragraphs to write, one string per paragraph. An "
                            "object with 'title' and/or 'paragraphs' is also "
                            "accepted, and an item may be an object with 'text' "
                            "and optional 'style'."
                        ),
                    },
                },
                "required": ["path", "paragraphs"],
            },
            fn=docx_write,
            dangerous=True,
        ),
    ]


FILES_PACK = CapabilityPack(PACK_MANIFEST, tools_factory=tools)
