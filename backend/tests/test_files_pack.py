"""Tests for the files capability pack.

What these tests defend
-----------------------
The pack is the only place a model can touch the disk, so the interesting
assertions are not "file_write returns a name" -- they are:

* the *workspace* is a real boundary: absolute paths, ``..`` traversals, NUL
  bytes, Windows device names and symlinks that leave the root are refused by
  every path-taking tool, and a refused write creates nothing;
* the document formats actually round-trip through the installed libraries:
  CSV with Chinese and a BOM, multi-sheet Excel with numbers and booleans, a
  PDF generated on the fly, and Word paragraphs -- no mocks, no sample files;
* writing goes through the permission policy and the audit trail, and fails
  closed when either refuses;
* importing the pack module does not import openpyxl / python-docx / pypdf, so
  the platform still starts on a machine where they are missing;
* the manifest and the code agree, and the pack can register its own tools.

Temporary workspace roots come from ``AGENT_WORKSPACE_DIR`` because
``workspace_from_env`` re-reads the environment on every call -- that is the
seam that makes a tool testable without a run context.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any

import pytest

from app.runtime.audit import (
    AuditError,
    AuditEvent,
    AuditRecorder,
    JsonlAuditRecorder,
    set_audit_recorder,
)
from app.runtime.packs.builtin import files as files_pack
from app.runtime.packs.builtin.files import (
    FILES_MANIFEST,
    FILES_PACK,
    PACK_MANIFEST,
    TOOL_NAMES,
)
from app.runtime.permissions import (
    AllowAllPolicy,
    AllowlistPolicy,
    DenyAllPolicy,
    FILES_LIST,
    FILES_READ,
    FILES_WRITE,
    PermissionDenied,
    set_permission_policy,
)
from app.runtime.workspace import Workspace
from app.tools.registry import ToolDef, ToolError

WRITE_TOOL_NAMES = frozenset({"file_write", "csv_write", "excel_write", "docx_write"})

#: Tools that take a ``path`` argument. The path-safety matrix runs every one of
#: them against every bad path, because a single tool that trusts its input is
#: enough to escape the workspace.
PATH_TOOLS = (
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

#: Paths that must never resolve inside the workspace (or anywhere else).
ESCAPING_PATHS = (
    "C:\\Windows\\win.ini",  # absolute, drive-qualified
    "/etc/passwd",  # rooted on the current drive
    "..\\escape.txt",  # parent traversal
    "sub/../../escape.txt",  # traversal hidden behind a sub-directory
    "a\x00b.txt",  # NUL byte
    "CON",  # Windows device name
    "con.txt",  # device name with an extension
    "nul.log",  # another device name
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _deterministic_capabilities(monkeypatch) -> Any:
    """Every test starts from the permissive default policy and no audit sink.

    Restored afterwards rather than merely overridden: both objects are
    process-wide, so a test that left a DenyAllPolicy behind would make an
    unrelated test fail for a reason invisible in its own body.
    """
    monkeypatch.setenv("AUDIT_LOG", "none")
    set_permission_policy(AllowAllPolicy())
    set_audit_recorder(None)
    yield
    set_permission_policy(None)
    set_audit_recorder(None)


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch) -> pathlib.Path:
    """A per-test workspace root, switched in through the environment."""
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(root))
    return root


def _tool(tool_name: str) -> ToolDef:
    for tool in FILES_PACK.tools():
        if tool.name == tool_name:
            return tool
    raise AssertionError(f"the pack has no tool '{tool_name}'")


async def _call(tool_name: str, **arguments: Any) -> Any:
    return await _tool(tool_name)(**arguments)


def _arguments_for(tool_name: str, path: str) -> dict[str, Any]:
    """The smallest valid call for ``tool_name`` with ``path`` substituted."""
    if tool_name == "file_write":
        return {"path": path, "content": "x"}
    if tool_name == "csv_write":
        return {"path": path, "rows": [["a"]]}
    if tool_name == "excel_write":
        return {"path": path, "rows": [{"a": 1}]}
    if tool_name == "docx_write":
        return {"path": path, "paragraphs": ["x"]}
    return {"path": path}


def _make_symlink_or_skip(target: pathlib.Path, link: pathlib.Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - filesystem dependent
        pytest.skip(f"symlinks are unavailable on this filesystem: {exc}")


def _read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    assert path.exists(), f"expected an audit file at {path}"
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return [json.loads(line) for line in lines]


class _BrokenRecorder(AuditRecorder):
    """A recorder that cannot append: the write must not happen without a record."""

    def record(self, event: AuditEvent) -> None:  # noqa: ARG002 - test double
        raise AuditError("simulated audit disk failure")


def _blank_pdf(page_count: int) -> bytes:
    """A real PDF made on the spot by pypdf: blank pages, no sample file."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _text_pdf(text: str) -> bytes:
    """A minimal PDF with one line of embedded text, assembled by hand.

    pypdf can create blank pages but cannot draw text, and the point of the PDF
    test is that *extraction* works. Building the five objects here keeps the
    test self-contained: no sample file in the repository, nothing to download.
    """
    stream = b"BT /F1 24 Tf 72 720 Td (" + text.encode("latin-1") + b") Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += str(number).encode("ascii") + b" 0 obj\n" + body + b"\nendobj\n"
    xref_at = len(output)
    output += b"xref\n0 " + str(len(objects) + 1).encode("ascii") + b"\n"
    output += b"0000000000 65535 f \n"
    for offset in offsets:
        output += ("%010d 00000 n \n" % offset).encode("ascii")
    output += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode("ascii")
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_at).encode("ascii")
        + b"\n%%EOF\n"
    )
    return bytes(output)


# ---------------------------------------------------------------------------
# Registration: the manifest and the code must agree
# ---------------------------------------------------------------------------


class TestPackRegistration:
    def test_the_builtin_registry_ships_the_files_pack(self) -> None:
        from app.runtime.packs.registry import BUILTIN_PACK_NAMES, builtin_registry

        assert "files" in BUILTIN_PACK_NAMES
        registry = builtin_registry()
        assert "files" in registry.names()
        pack = registry.get("files")
        assert pack.manifest.name == "files"
        assert pack.manifest.version == "1.0.0"
        assert pack.problems() == []

    def test_the_manifest_and_the_code_agree_on_the_tools(self) -> None:
        provided = tuple(tool.name for tool in FILES_PACK.tools())
        assert FILES_MANIFEST.tools == TOOL_NAMES
        assert provided == TOOL_NAMES
        assert len(set(TOOL_NAMES)) == len(TOOL_NAMES) == 10
        assert FILES_MANIFEST is PACK_MANIFEST
        assert set(TOOL_NAMES) == {
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
        }

    def test_the_manifest_declares_permissions_and_dependencies(self) -> None:
        assert FILES_MANIFEST.permissions == (FILES_LIST, FILES_READ, FILES_WRITE)
        assert FILES_MANIFEST.secrets == ()
        assert [dependency.spec for dependency in FILES_MANIFEST.dependencies] == [
            "openpyxl>=3.1",
            "python-docx>=1.1",
            "pypdf>=5",
        ]
        assert {
            dependency.import_name for dependency in FILES_MANIFEST.dependencies
        } == {"openpyxl", "docx", "pypdf"}
        for field in ("title", "title_zh", "description", "description_zh", "notes"):
            assert getattr(FILES_MANIFEST, field), field
        assert "workspace" in FILES_MANIFEST.notes.lower()

    def test_the_registry_maps_every_tool_to_the_pack(self) -> None:
        from app.runtime.packs.registry import builtin_registry

        registry = builtin_registry()
        for name in TOOL_NAMES:
            assert registry.pack_for_tool(name) == "files", name
        entry = next(item for item in registry.manifests() if item["name"] == "files")
        assert entry["tools"] == list(TOOL_NAMES)

    def test_the_pack_can_register_its_tools_into_a_registry(self) -> None:
        from app.runtime.packs.registry import builtin_registry
        from app.tools.registry import BuiltinToolRegistry

        target = BuiltinToolRegistry(store={})
        registered = builtin_registry().register_tools(target, packs=["files"])

        assert set(registered) == set(TOOL_NAMES)
        assert set(target.names()) == set(TOOL_NAMES)

    def test_every_tool_has_a_valid_openai_schema(self) -> None:
        for tool in FILES_PACK.tools():
            assert isinstance(tool, ToolDef)
            assert re.fullmatch(r"[a-z][a-z0-9_]*", tool.name)
            assert tool.label and tool.label_zh
            assert tool.description and tool.description_zh
            assert inspect.iscoroutinefunction(tool.fn)
            schema = tool.openai_schema()
            assert schema["type"] == "function"
            function = schema["function"]
            assert function["name"] == tool.name
            assert function["description"]
            parameters = function["parameters"]
            assert parameters["type"] == "object"
            properties = parameters.get("properties", {})
            assert isinstance(properties, dict)
            for property_name, spec in properties.items():
                assert isinstance(spec, dict), property_name
                assert spec.get("description"), f"{tool.name}.{property_name}"
            required = parameters.get("required", [])
            assert isinstance(required, list)
            assert set(required) <= set(properties)

    def test_only_writing_tools_are_flagged_dangerous(self) -> None:
        flagged = {tool.name for tool in FILES_PACK.tools() if tool.dangerous}
        assert flagged == WRITE_TOOL_NAMES
        assert files_pack.WRITE_TOOL_NAMES == WRITE_TOOL_NAMES

    def test_the_document_libraries_are_imported_lazily(self) -> None:
        source = pathlib.Path(files_pack.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=files_pack.__file__)
        top_level: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module)
        heavy = {"openpyxl", "docx", "pypdf"}
        assert not (heavy & top_level), (
            "module-level imports would require the library at import time: "
            f"{sorted(heavy & top_level)}"
        )
        everywhere: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                everywhere.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                everywhere.add(node.module)
        assert heavy <= everywhere, (
            "expected the lazy imports to exist inside the tool functions; "
            f"found {sorted(heavy & everywhere)}"
        )

    def test_the_module_stays_inside_the_runtime_layer_boundary(self) -> None:
        """Mirrors tests/test_layer_boundary.py for this module specifically.

        The runtime is copied into an exported agent as source, so a lazy
        ``app.db`` import would travel and fail on a customer's machine.
        """
        source = pathlib.Path(files_pack.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=files_pack.__file__)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = (
            "app.api",
            "app.db",
            "app.main",
            "fastapi",
            "sqlmodel",
            "sqlalchemy",
            "alembic",
            "starlette",
        )
        offenders = sorted(
            module
            for module in imported
            for prefix in forbidden
            if module == prefix or module.startswith(prefix + ".")
        )
        assert offenders == []

    def test_importing_the_pack_does_not_import_the_document_libraries(self) -> None:
        """The doctor-report promise, checked in a clean interpreter.

        In-process would lie: an earlier Excel test imports openpyxl, so
        ``sys.modules`` no longer says anything about *this module's* imports.
        """
        backend = pathlib.Path(__file__).resolve().parents[1]
        code = (
            "import sys\n"
            "import app.runtime.packs.builtin.files as files\n"
            "assert files.PACK_MANIFEST.name == 'files'\n"
            "loaded = [name for name in ('openpyxl', 'docx', 'pypdf') "
            "if name in sys.modules]\n"
            "assert not loaded, loaded\n"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = (
            str(backend)
            if not environment.get("PYTHONPATH")
            else str(backend) + os.pathsep + environment["PYTHONPATH"]
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(backend),
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The workspace boundary
# ---------------------------------------------------------------------------


class TestWorkspacePathSafety:
    @pytest.mark.parametrize("bad_path", ESCAPING_PATHS)
    @pytest.mark.parametrize("tool_name", PATH_TOOLS)
    async def test_no_tool_accepts_a_path_that_escapes_the_workspace(
        self, tool_name: str, bad_path: str, workspace_dir: pathlib.Path
    ) -> None:
        with pytest.raises(ToolError):
            await _call(tool_name, **_arguments_for(tool_name, bad_path))

    async def test_a_refused_write_creates_nothing_on_the_host(
        self, workspace_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        with pytest.raises(ToolError):
            await _call("file_write", path="../escaped.txt", content="x")

        assert not (tmp_path / "escaped.txt").exists()

    async def test_a_symlink_pointing_outside_is_refused(
        self, workspace_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("host secret", encoding="utf-8")
        _make_symlink_or_skip(secret, workspace_dir / "innocent.txt")

        with pytest.raises(ToolError, match="outside the workspace"):
            await _call("file_read", path="innocent.txt")
        with pytest.raises(ToolError, match="outside the workspace"):
            await _call("file_write", path="innocent.txt", content="overwrite")

        assert secret.read_text(encoding="utf-8") == "host secret"

    async def test_a_symlinked_directory_cannot_be_used_to_escape(
        self, workspace_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        outside = tmp_path / "outside_dir"
        outside.mkdir()
        _make_symlink_or_skip(outside, workspace_dir / "docs")

        with pytest.raises(ToolError):
            await _call("file_write", path="docs/planted.txt", content="x")

        assert not (outside / "planted.txt").exists()

    @pytest.mark.parametrize(
        "bad_pattern",
        ["../*", "sub/../../*", "C:/*", "/etc/*", "\\\\server\\share\\*"],
    )
    async def test_file_list_refuses_globs_that_escape(
        self, workspace_dir: pathlib.Path, bad_pattern: str
    ) -> None:
        with pytest.raises(ToolError):
            await _call("file_list", pattern=bad_pattern)

    @pytest.mark.parametrize("bad_directory", ["..", "C:\\Windows", "/etc", "CON"])
    async def test_file_list_refuses_directories_that_escape(
        self, workspace_dir: pathlib.Path, bad_directory: str
    ) -> None:
        with pytest.raises(ToolError):
            await _call("file_list", directory=bad_directory)


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------


class TestPlainFiles:
    async def test_write_then_read_round_trip(self, workspace_dir: pathlib.Path) -> None:
        content = "你好，world\nsecond line"
        written = await _call("file_write", path="notes.txt", content=content)
        assert written == {"name": "notes.txt", "size": len(content.encode("utf-8"))}
        assert (workspace_dir / "notes.txt").read_text(encoding="utf-8") == content

        read = await _call("file_read", path="notes.txt")
        assert read == {"name": "notes.txt", "content": content}

    async def test_write_creates_parent_directories(
        self, workspace_dir: pathlib.Path
    ) -> None:
        written = await _call("file_write", path="reports/2026/q1.txt", content="q1")

        assert written["name"] == "reports/2026/q1.txt"
        assert (
            workspace_dir / "reports" / "2026" / "q1.txt"
        ).read_text(encoding="utf-8") == "q1"

    async def test_overwrite_false_refuses_to_replace(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call("file_write", path="keep.txt", content="first")

        with pytest.raises(ToolError, match="overwrite is off"):
            await _call("file_write", path="keep.txt", content="second", overwrite=False)

        assert (workspace_dir / "keep.txt").read_text(encoding="utf-8") == "first"

    async def test_list_reports_name_size_and_modified(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call("file_write", path="a.txt", content="abc")

        listing = await _call("file_list")

        assert [entry["name"] for entry in listing["files"]] == ["a.txt"]
        entry = listing["files"][0]
        assert entry["size"] == 3
        assert isinstance(entry["modified"], (int, float))

    async def test_list_is_recursive_and_filters_at_each_level(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call("file_write", path="data/a.csv", content="x")
        await _call("file_write", path="data/b.txt", content="y")
        await _call("file_write", path="other/c.csv", content="z")

        everything = await _call("file_list")
        assert [entry["name"] for entry in everything["files"]] == [
            "data/a.csv",
            "data/b.txt",
            "other/c.csv",
        ]

        in_data = await _call("file_list", directory="data")
        assert [entry["name"] for entry in in_data["files"]] == [
            "data/a.csv",
            "data/b.txt",
        ]

        csv_only = await _call("file_list", pattern="*.csv")
        assert [entry["name"] for entry in csv_only["files"]] == [
            "data/a.csv",
            "other/c.csv",
        ]

    async def test_a_missing_file_lists_what_does_exist(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call("file_write", path="known.txt", content="x")

        with pytest.raises(ToolError) as excinfo:
            await _call("file_read", path="missing.txt")

        message = str(excinfo.value)
        assert "missing.txt" in message
        assert "known.txt" in message

    async def test_read_respects_the_per_call_byte_cap(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call("file_write", path="ten.txt", content="0123456789")

        with pytest.raises(ToolError, match="at most 4 bytes"):
            await _call("file_read", path="ten.txt", max_bytes=4)

        assert (await _call("file_read", path="ten.txt", max_bytes=10))[
            "content"
        ] == "0123456789"

    async def test_a_workspace_is_created_on_first_use(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        root = tmp_path / "fresh" / "workspace"
        monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(root))

        await _call("file_write", path="first.txt", content="hello")

        assert (root / "first.txt").read_text(encoding="utf-8") == "hello"


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


class TestCsv:
    async def test_round_trip_of_chinese_columns_and_values(
        self, workspace_dir: pathlib.Path
    ) -> None:
        rows = [["张三", 90, "优秀"], ["李四", 88.5, "良好"]]
        written = await _call(
            "csv_write",
            path="scores.csv",
            columns=["姓名", "分数", "评价"],
            rows=rows,
        )

        assert written["name"] == "scores.csv"
        # Bytes, not read_text: text mode translates CRLF to LF, which would
        # make this assertion pass for the wrong file format.
        raw_text = (workspace_dir / "scores.csv").read_bytes().decode("utf-8")
        assert raw_text.startswith("姓名,分数,评价\r\n")
        assert raw_text.count("\r\n") == 3

        read = await _call("csv_read", path="scores.csv")
        assert read["columns"] == ["姓名", "分数", "评价"]
        assert read["rows"] == [["张三", "90", "优秀"], ["李四", "88.5", "良好"]]
        assert read["total_rows"] == 2
        assert read["truncated"] is False

    async def test_utf8_bom_is_stripped_from_the_header(
        self, workspace_dir: pathlib.Path
    ) -> None:
        (workspace_dir / "bom.csv").write_bytes(
            "\ufeffname,score\n张三,90\n".encode("utf-8")
        )

        read = await _call("csv_read", path="bom.csv")

        assert read["columns"] == ["name", "score"]
        assert read["rows"] == [["张三", "90"]]

    async def test_gb18030_content_is_decoded(self, workspace_dir: pathlib.Path) -> None:
        (workspace_dir / "gbk.csv").write_bytes(
            "姓名,分数\r\n王五,77\r\n".encode("gb18030")
        )

        read = await _call("csv_read", path="gbk.csv")

        assert read["columns"] == ["姓名", "分数"]
        assert read["rows"] == [["王五", "77"]]

    async def test_limit_truncates_but_total_rows_counts_the_file(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call(
            "csv_write",
            path="many.csv",
            columns=["label"],
            rows=[[f"row{index}"] for index in range(5)],
        )

        truncated = await _call("csv_read", path="many.csv", limit=2)
        assert truncated["columns"] == ["label"]
        assert truncated["rows"] == [["row0"], ["row1"]]
        assert truncated["total_rows"] == 5
        assert truncated["truncated"] is True

        full = await _call("csv_read", path="many.csv", limit=50)
        assert full["truncated"] is False
        assert len(full["rows"]) == 5

    async def test_custom_delimiter(self, workspace_dir: pathlib.Path) -> None:
        (workspace_dir / "semi.csv").write_text("a;b\n1;2\n", encoding="utf-8")

        read = await _call("csv_read", path="semi.csv", delimiter=";")

        assert read["columns"] == ["a", "b"]
        assert read["rows"] == [["1", "2"]]

    async def test_a_multi_character_delimiter_is_refused(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call("csv_write", path="plain.csv", rows=[["1"]])

        with pytest.raises(ToolError, match="single character"):
            await _call("csv_read", path="plain.csv", delimiter=";;")

    async def test_malformed_rows_are_refused(self, workspace_dir: pathlib.Path) -> None:
        with pytest.raises(ToolError, match="must be a list of values"):
            await _call("csv_write", path="bad.csv", rows=["not a row"])


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


class TestExcel:
    async def test_round_trip_of_sheets_chinese_numbers_and_booleans(
        self, workspace_dir: pathlib.Path
    ) -> None:
        written = await _call(
            "excel_write",
            path="report.xlsx",
            sheets={
                "数据": [
                    {"姓名": "张三", "分数": 90, "及格": True},
                    {"姓名": "李四", "分数": 88.5, "及格": False},
                ],
                "汇总": [{"指标": "人数", "值": 2}],
            },
        )

        assert written["name"] == "report.xlsx"
        assert written["sheets"] == ["数据", "汇总"]
        assert (workspace_dir / "report.xlsx").is_file()

        read = await _call("excel_read", path="report.xlsx")
        assert read["sheet"] == "数据"
        assert read["sheets"] == ["数据", "汇总"]
        assert read["columns"] == ["姓名", "分数", "及格"]
        assert read["total_rows"] == 2
        assert read["truncated"] is False
        assert read["rows"][0] == {"姓名": "张三", "分数": 90, "及格": True}
        assert read["rows"][1] == {"姓名": "李四", "分数": 88.5, "及格": False}

    async def test_reading_a_named_sheet(self, workspace_dir: pathlib.Path) -> None:
        await _call(
            "excel_write",
            path="book.xlsx",
            sheets={"数据": [{"a": 1}], "汇总": [{"指标": "人数", "值": 2}]},
        )

        read = await _call("excel_read", path="book.xlsx", sheet="汇总")

        assert read["sheet"] == "汇总"
        assert read["columns"] == ["指标", "值"]
        assert read["rows"] == [{"指标": "人数", "值": 2}]

    async def test_an_unknown_sheet_names_the_available_ones(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call("excel_write", path="book.xlsx", sheets={"数据": [{"a": 1}]})

        with pytest.raises(ToolError) as excinfo:
            await _call("excel_read", path="book.xlsx", sheet="Nope")

        message = str(excinfo.value)
        assert "Nope" in message
        assert "数据" in message

    async def test_header_row_can_be_chosen(self, workspace_dir: pathlib.Path) -> None:
        await _call(
            "excel_write",
            path="titled.xlsx",
            rows=[["月度报表"], ["月份", "收入"], ["一月", 10], ["二月", 20]],
        )

        read = await _call("excel_read", path="titled.xlsx", header_row=2)

        assert read["columns"] == ["月份", "收入"]
        assert read["rows"] == [
            {"月份": "一月", "收入": 10},
            {"月份": "二月", "收入": 20},
        ]
        assert read["total_rows"] == 2

    async def test_limit_truncates_but_total_rows_counts_the_sheet(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call(
            "excel_write",
            path="many.xlsx",
            rows=[{"n": index} for index in range(5)],
        )

        truncated = await _call("excel_read", path="many.xlsx", limit=2)

        assert len(truncated["rows"]) == 2
        assert truncated["total_rows"] == 5
        assert truncated["truncated"] is True

    async def test_object_rows_union_their_columns_in_first_seen_order(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call(
            "excel_write",
            path="union.xlsx",
            rows=[{"a": 1}, {"a": 2, "b": 3}],
        )

        read = await _call("excel_read", path="union.xlsx")

        assert read["columns"] == ["a", "b"]
        assert read["rows"] == [{"a": 1, "b": None}, {"a": 2, "b": 3}]

    async def test_a_non_excel_file_reports_a_readable_error(
        self, workspace_dir: pathlib.Path
    ) -> None:
        (workspace_dir / "fake.xlsx").write_bytes(b"this is not a workbook")

        with pytest.raises(ToolError, match="not a readable"):
            await _call("excel_read", path="fake.xlsx")


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


class TestPdf:
    async def test_reads_a_pdf_generated_with_pypdf(
        self, workspace_dir: pathlib.Path
    ) -> None:
        (workspace_dir / "blank.pdf").write_bytes(_blank_pdf(3))

        data = await _call("pdf_read", path="blank.pdf")

        assert data["total_pages"] == 3
        assert [page["page"] for page in data["pages"]] == [1, 2, 3]
        assert all(page["text"] == "" for page in data["pages"])

    async def test_max_pages_caps_the_pages_but_reports_the_total(
        self, workspace_dir: pathlib.Path
    ) -> None:
        (workspace_dir / "blank.pdf").write_bytes(_blank_pdf(4))

        data = await _call("pdf_read", path="blank.pdf", max_pages=2)

        assert len(data["pages"]) == 2
        assert data["total_pages"] == 4

    async def test_extracts_embedded_text(self, workspace_dir: pathlib.Path) -> None:
        (workspace_dir / "hello.pdf").write_bytes(_text_pdf("Hello PDF"))

        data = await _call("pdf_read", path="hello.pdf")

        assert data["total_pages"] == 1
        assert "Hello PDF" in data["pages"][0]["text"]

    async def test_a_corrupt_pdf_reports_a_readable_error(
        self, workspace_dir: pathlib.Path
    ) -> None:
        (workspace_dir / "broken.pdf").write_bytes(b"definitely not a pdf")

        with pytest.raises(ToolError, match="not a readable PDF"):
            await _call("pdf_read", path="broken.pdf")


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------


class TestDocx:
    async def test_round_trip_of_a_paragraph_list(
        self, workspace_dir: pathlib.Path
    ) -> None:
        written = await _call(
            "docx_write",
            path="letter.docx",
            paragraphs=["第一段", "第二段 with ascii"],
        )

        assert written["name"] == "letter.docx"
        assert written["paragraphs"] == 2

        read = await _call("docx_read", path="letter.docx")
        assert read["paragraphs"] == ["第一段", "第二段 with ascii"]
        assert read["text"] == "第一段\n第二段 with ascii"

    async def test_a_title_object_is_written_as_the_first_paragraph(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call(
            "docx_write",
            path="report.docx",
            paragraphs={"title": "季度报告", "paragraphs": ["正文一", "正文二"]},
        )

        read = await _call("docx_read", path="report.docx")

        assert read["paragraphs"] == ["季度报告", "正文一", "正文二"]

    async def test_max_paragraphs_caps_the_read(
        self, workspace_dir: pathlib.Path
    ) -> None:
        await _call(
            "docx_write",
            path="many.docx",
            paragraphs=[f"P{index}" for index in range(5)],
        )

        read = await _call("docx_read", path="many.docx", max_paragraphs=2)

        assert read["paragraphs"] == ["P0", "P1"]
        assert read["text"] == "P0\nP1"

    async def test_an_unknown_style_is_reported(
        self, workspace_dir: pathlib.Path
    ) -> None:
        with pytest.raises(ToolError, match="unknown paragraph style"):
            await _call(
                "docx_write",
                path="styled.docx",
                paragraphs=[{"text": "x", "style": "No Such Style"}],
            )

    async def test_a_corrupt_docx_reports_a_readable_error(
        self, workspace_dir: pathlib.Path
    ) -> None:
        (workspace_dir / "broken.docx").write_bytes(b"definitely not a docx")

        with pytest.raises(ToolError, match="not a readable"):
            await _call("docx_read", path="broken.docx")


# ---------------------------------------------------------------------------
# Size limits
# ---------------------------------------------------------------------------


class TestWorkspaceLimits:
    async def test_a_write_larger_than_the_deployment_cap_is_refused(
        self, workspace_dir: pathlib.Path, monkeypatch
    ) -> None:
        limited = Workspace(workspace_dir, max_write_bytes=8)
        monkeypatch.setattr(files_pack, "_workspace", lambda: limited)

        with pytest.raises(ToolError, match="at most 8 bytes"):
            await _call("file_write", path="too-big.txt", content="x" * 100)

        assert not (workspace_dir / "too-big.txt").exists()

    async def test_the_cap_also_applies_to_document_writes(
        self, workspace_dir: pathlib.Path, monkeypatch
    ) -> None:
        limited = Workspace(workspace_dir, max_write_bytes=32)
        monkeypatch.setattr(files_pack, "_workspace", lambda: limited)

        with pytest.raises(ToolError, match="at most 32 bytes"):
            await _call("csv_write", path="big.csv", columns=["c"], rows=[["x" * 100]])

        assert not (workspace_dir / "big.csv").exists()


# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------


class TestPermissions:
    async def test_a_deny_all_policy_blocks_writes_without_writing(
        self, workspace_dir: pathlib.Path
    ) -> None:
        set_permission_policy(DenyAllPolicy())

        with pytest.raises(PermissionDenied) as excinfo:
            await _call("file_write", path="denied.txt", content="should not exist")

        assert excinfo.value.permission == FILES_WRITE
        assert not (workspace_dir / "denied.txt").exists()

    async def test_a_read_only_allowlist_blocks_every_writing_tool(
        self, workspace_dir: pathlib.Path
    ) -> None:
        set_permission_policy(AllowlistPolicy([FILES_LIST, FILES_READ]))
        attempts = (
            ("file_write", {"path": "a.txt", "content": "x"}),
            ("csv_write", {"path": "a.csv", "columns": ["c"], "rows": [["1"]]}),
            ("excel_write", {"path": "a.xlsx", "rows": [{"c": 1}]}),
            ("docx_write", {"path": "a.docx", "paragraphs": ["x"]}),
        )

        for tool_name, arguments in attempts:
            with pytest.raises(PermissionDenied) as excinfo:
                await _call(tool_name, **arguments)
            assert excinfo.value.permission == FILES_WRITE
            assert "files.write" in str(excinfo.value)

        assert list(workspace_dir.iterdir()) == []

    async def test_a_read_only_allowlist_still_permits_reads_and_listing(
        self, workspace_dir: pathlib.Path
    ) -> None:
        (workspace_dir / "existing.txt").write_text("ok", encoding="utf-8")
        set_permission_policy(AllowlistPolicy([FILES_LIST, FILES_READ]))

        assert (await _call("file_read", path="existing.txt"))["content"] == "ok"
        assert [entry["name"] for entry in (await _call("file_list"))["files"]] == [
            "existing.txt"
        ]

    async def test_deny_all_blocks_reads_and_listing_as_well(
        self, workspace_dir: pathlib.Path
    ) -> None:
        (workspace_dir / "existing.txt").write_text("ok", encoding="utf-8")
        set_permission_policy(DenyAllPolicy())

        with pytest.raises(PermissionDenied) as read_denied:
            await _call("file_read", path="existing.txt")
        assert read_denied.value.permission == FILES_READ

        with pytest.raises(PermissionDenied) as list_denied:
            await _call("file_list")
        assert list_denied.value.permission == FILES_LIST


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


class TestAudit:
    async def test_file_write_records_action_tool_target_and_outcome(
        self, workspace_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        audit_path = tmp_path / "audit.jsonl"
        set_audit_recorder(JsonlAuditRecorder(audit_path))

        await _call("file_write", path="audited.txt", content="hello")

        writes = [
            event for event in _read_jsonl(audit_path) if event["action"] == "file.write"
        ]
        assert writes, "expected a file.write audit entry"
        event = writes[-1]
        assert event["tool"] == "file_write"
        assert event["target"] == "audited.txt"
        assert event["outcome"] == "ok"
        assert event["detail"]["size"] == 5
        assert event["at"]

    async def test_every_writing_tool_records_its_own_action(
        self, workspace_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        audit_path = tmp_path / "audit.jsonl"
        set_audit_recorder(JsonlAuditRecorder(audit_path))

        await _call("file_write", path="a.txt", content="x")
        await _call("csv_write", path="a.csv", columns=["c"], rows=[["1"]])
        await _call("excel_write", path="a.xlsx", rows=[{"c": 1}])
        await _call("docx_write", path="a.docx", paragraphs=["p"])

        actions = [event["action"] for event in _read_jsonl(audit_path)]
        assert actions == [
            "file.write",
            "file.csv_write",
            "file.excel_write",
            "file.docx_write",
        ]

    async def test_file_list_is_audited(
        self, workspace_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        audit_path = tmp_path / "audit.jsonl"
        set_audit_recorder(JsonlAuditRecorder(audit_path))

        await _call("file_list")

        events = _read_jsonl(audit_path)
        assert [event["action"] for event in events] == ["file.list"]
        assert events[0]["tool"] == "file_list"
        assert events[0]["outcome"] == "ok"
        assert events[0]["target"] == "*"

    async def test_the_audit_entry_is_written_before_the_file(
        self, workspace_dir: pathlib.Path
    ) -> None:
        target = workspace_dir / "ordered.txt"

        class _OrderProbe(AuditRecorder):
            def __init__(self) -> None:
                self.existed_at_record: bool | None = None

            def record(self, event: AuditEvent) -> None:  # noqa: ARG002 - test double
                self.existed_at_record = target.exists()

        probe = _OrderProbe()
        set_audit_recorder(probe)

        await _call("file_write", path="ordered.txt", content="x")

        assert probe.existed_at_record is False
        assert target.is_file()

    async def test_an_audit_failure_prevents_the_write(
        self, workspace_dir: pathlib.Path
    ) -> None:
        set_audit_recorder(_BrokenRecorder())

        with pytest.raises(AuditError):
            await _call("file_write", path="unrecorded.txt", content="x")

        assert not (workspace_dir / "unrecorded.txt").exists()


# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------


class TestOptionalDependencies:
    async def test_a_missing_openpyxl_becomes_a_clear_tool_error(
        self, workspace_dir: pathlib.Path, monkeypatch
    ) -> None:
        (workspace_dir / "book.xlsx").write_bytes(b"placeholder")
        monkeypatch.setitem(sys.modules, "openpyxl", None)

        with pytest.raises(ToolError, match="openpyxl"):
            await _call("excel_read", path="book.xlsx")

    async def test_a_missing_pypdf_becomes_a_clear_tool_error(
        self, workspace_dir: pathlib.Path, monkeypatch
    ) -> None:
        (workspace_dir / "file.pdf").write_bytes(b"placeholder")
        monkeypatch.setitem(sys.modules, "pypdf", None)

        with pytest.raises(ToolError, match="pypdf"):
            await _call("pdf_read", path="file.pdf")

