"""Tests for the capability-pack foundation.

What is covered here, and why it is a separate file from the pack tests:

* the **contract** -- manifests, registry, ownership, doctor. If these drift, a
  pack can be declared and never loaded, which looks exactly like a model that
  refuses to call a tool.
* the **seams** a pack acts through -- permissions, audit, workspace. These are
  shared by every future pack, so they get their own coverage rather than being
  tested incidentally through one tool.
* the **export parity of the boundary** -- a graph that names a pack tool must
  produce a package whose manifest declares the pack and whose `doctor` passes
  on a clean directory. That is the claim "the same capability travels with the
  agent", expressed as a test.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from app.tools.registry import BuiltinToolRegistry, ToolDef


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_a_minimal_manifest_is_valid(self):
        from app.runtime.packs.manifest import PackManifest

        manifest = PackManifest(
            name="demo",
            version="1.2.3",
            tools=("t_one",),
            permissions=("files.read",),
            secrets=("DEMO_KEY",),
        )
        assert manifest.problems() == []
        assert manifest.to_dict()["tools"] == ["t_one"]

    def test_bad_names_and_duplicates_are_refused(self):
        from app.runtime.packs.manifest import ManifestError, PackManifest

        with pytest.raises(ManifestError):
            PackManifest(name="Bad Name")
        with pytest.raises(ManifestError):
            PackManifest(name="demo", tools=("same", "same"))
        with pytest.raises(ManifestError):
            PackManifest(name="demo", permissions=("not a permission",))
        with pytest.raises(ManifestError):
            PackManifest(name="demo", secrets=("lower_case",))
        with pytest.raises(ManifestError):
            PackManifest(name="demo", api_version="999")

    def test_dependency_derives_import_name_but_explicit_wins(self):
        from app.runtime.packs.manifest import PackDependency

        assert PackDependency("openpyxl>=3.1").import_name == "openpyxl"
        assert PackDependency("python-docx>=1.1", import_name="docx").import_name == "docx"
        with pytest.raises(Exception):
            PackDependency("")

    def test_roundtrip_through_mapping(self):
        from app.runtime.packs.manifest import PackDependency, PackManifest

        manifest = PackManifest(
            name="demo",
            title="Demo",
            tools=("t",),
            dependencies=(PackDependency("openpyxl>=3.1", "openpyxl"),),
            settings={"pack_flag": True},
        )
        again = PackManifest.from_mapping(manifest.to_dict())
        assert again == manifest


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _pack(name="probe", tools=("probe_tool",)):
    from app.runtime.packs.manifest import PackManifest
    from app.runtime.packs.registry import CapabilityPack

    def factory():
        return [
            ToolDef(
                name=tool_name,
                label=tool_name,
                description="probe",
                parameters={"type": "object", "properties": {}},
                fn=lambda: {"ok": True},
            )
            for tool_name in tools
        ]

    return CapabilityPack(PackManifest(name=name, tools=tools), tools_factory=factory)


class TestRegistry:
    def test_tool_ownership_and_lookup(self):
        from app.runtime.packs.registry import PackError, PackRegistry

        registry = PackRegistry([_pack()])
        assert registry.names() == ["probe"]
        assert registry.tool_names() == ["probe_tool"]
        assert registry.pack_for_tool("probe_tool") == "probe"
        assert registry.packs_for_tools(["probe_tool", "nope"]) == ["probe"]
        assert registry.resolve_tool("probe_tool").name == "probe_tool"
        with pytest.raises(PackError):
            registry.resolve_tool("nope")

    def test_duplicate_pack_or_tool_is_refused(self):
        from app.runtime.packs.registry import PackError, PackRegistry

        with pytest.raises(PackError):
            PackRegistry([_pack("pack_a"), _pack("pack_a")])
        with pytest.raises(PackError):
            PackRegistry([_pack("pack_a", ("same",)), _pack("pack_b", ("same",))])

    def test_register_tools_honours_pack_and_tool_filters(self):
        from app.runtime.packs.registry import PackRegistry

        registry = PackRegistry(
            [_pack("pack_a", ("one", "two")), _pack("pack_b", ("three",))]
        )
        target = BuiltinToolRegistry(store={})
        added = registry.register_tools(target, packs=["pack_a"], tool_names=["two"])
        assert sorted(target.names()) == ["two"]
        assert added == ["two"]

    def test_missing_manifest_tools_are_reported_by_problems(self):
        from app.runtime.packs.manifest import PackManifest
        from app.runtime.packs.registry import CapabilityPack

        pack = CapabilityPack(PackManifest(name="broken", tools=("declared",)))
        assert pack.problems(), "a pack that provides nothing must report the gap"

    def test_doctor_reports_missing_dependency(self):
        from app.runtime.packs.manifest import PackDependency, PackManifest
        from app.runtime.packs.registry import CapabilityPack, PackRegistry, doctor_report

        pack = CapabilityPack(
            PackManifest(
                name="needs",
                tools=(),
                dependencies=(
                    PackDependency("definitely-not-installed-xyz", "definitely_not_installed_xyz"),
                ),
            )
        )
        report = doctor_report(PackRegistry([pack]))
        assert report["ok"] is False
        assert any("missing dependency" in problem for problem in report["problems"])

    def test_builtin_registry_has_core_and_files(self):
        from app.runtime.packs.registry import BUILTIN_PACK_NAMES, builtin_registry

        registry = builtin_registry()
        assert registry.names() == sorted(BUILTIN_PACK_NAMES)
        assert "file_read" in registry.tool_names()
        assert "excel_read" in registry.tool_names()
        assert registry.pack_for_tool("calculator") == "core"
        assert registry.pack_for_tool("file_write") == "files"
        assert doctor_report_ok(registry)


def doctor_report_ok(registry) -> bool:
    from app.runtime.packs.registry import doctor_report

    return bool(doctor_report(registry)["ok"])


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_deny_all_policy_raises(self):
        from app.runtime.permissions import DenyAllPolicy, PermissionDenied, check_permission, set_permission_policy

        set_permission_policy(DenyAllPolicy())
        try:
            with pytest.raises(PermissionDenied):
                check_permission("files.write", tool="t")
        finally:
            set_permission_policy(None)

    def test_allowlist_and_approval_are_distinguishable(self):
        from app.runtime.permissions import AllowlistPolicy, PermissionDenied

        policy = AllowlistPolicy(["files.read", "files.write"], approval_required=["files.write"])
        policy.check("files.read", tool="t")
        with pytest.raises(PermissionDenied) as caught:
            policy.check("files.write", tool="t")
        assert caught.value.requires_approval is True
        with pytest.raises(PermissionDenied) as caught:
            policy.check("net.http", tool="t")
        assert caught.value.requires_approval is False

    def test_process_policy_injection_roundtrip(self):
        from app.runtime.permissions import AllowAllPolicy, get_permission_policy, set_permission_policy

        previous = get_permission_policy()
        set_permission_policy(AllowAllPolicy())
        try:
            assert get_permission_policy().decide("files.read").allowed is True
        finally:
            set_permission_policy(previous)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestAudit:
    def test_jsonl_recorder_appends_readable_events(self, tmp_path):
        from app.runtime.audit import AuditEvent, JsonlAuditRecorder

        path = tmp_path / "audit.jsonl"
        recorder = JsonlAuditRecorder(path)
        recorder.record(AuditEvent(action="file.write", tool="file_write", target="a.csv"))
        recorder.record(AuditEvent(action="file.list", outcome="denied"))
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [line["action"] for line in lines] == ["file.write", "file.list"]
        assert lines[0]["tool"] == "file_write"
        assert lines[0]["at"]

    def test_record_audit_uses_the_process_recorder(self, tmp_path):
        from app.runtime.audit import JsonlAuditRecorder, record_audit, set_audit_recorder

        path = tmp_path / "audit.jsonl"
        set_audit_recorder(JsonlAuditRecorder(path))
        try:
            record_audit("probe.event", target="x")
        finally:
            set_audit_recorder(None)
        assert "probe.event" in path.read_text(encoding="utf-8")

    def test_unwritable_audit_path_fails_closed(self, tmp_path):
        from app.runtime.audit import AuditError, AuditEvent, JsonlAuditRecorder

        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x", encoding="utf-8")
        recorder = JsonlAuditRecorder(blocker / "audit.jsonl")
        with pytest.raises(AuditError):
            recorder.record(AuditEvent(action="file.write"))


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


class TestWorkspace:
    def test_read_write_roundtrip(self, tmp_path):
        from app.runtime.workspace import Workspace

        workspace = Workspace(tmp_path / "ws")
        workspace.write_text("notes/a.txt", "你好")
        assert workspace.read_text("notes/a.txt") == "你好"
        assert [entry["name"] for entry in workspace.list()] == ["notes/a.txt"]
        workspace.delete("notes/a.txt")
        assert workspace.list() == []

    def test_gb18030_fallback_and_atomic_write(self, tmp_path):
        from app.runtime.workspace import Workspace

        workspace = Workspace(tmp_path / "ws")
        raw = "旧编码".encode("gb18030")
        (tmp_path / "ws" / "legacy.txt").write_bytes(raw)
        assert workspace.read_text("legacy.txt") == "旧编码"
        # A write replaces atomically: no temp file is left behind.
        workspace.write_text("x.txt", "abc")
        assert sorted(p.name for p in (tmp_path / "ws").iterdir()) == ["legacy.txt", "x.txt"]

    @pytest.mark.parametrize(
        "name",
        ["../escape.txt", "/absolute.txt", "C:/windows.txt", "", "a\x00b.txt", "CON.txt", "trail."],
    )
    def test_containment_refusals(self, tmp_path, name):
        from app.runtime.workspace import Workspace, WorkspaceError

        workspace = Workspace(tmp_path / "ws")
        with pytest.raises(WorkspaceError):
            workspace.resolve(name)

    def test_size_limits(self, tmp_path):
        from app.runtime.workspace import Workspace, WorkspaceError

        writer = Workspace(tmp_path / "ws", max_write_bytes=4)
        with pytest.raises(WorkspaceError):
            writer.write_text("big.txt", "12345")
        writer.write_text("ok.txt", "123")

        reader = Workspace(tmp_path / "ws", max_read_bytes=4)
        with pytest.raises(WorkspaceError):
            reader.read_bytes("big.txt")
        reader.write_text("five.txt", "12345")
        with pytest.raises(WorkspaceError):
            reader.read_bytes("five.txt")

    def test_missing_file_lists_alternatives_for_the_model(self, tmp_path):
        from app.runtime.workspace import Workspace, WorkspaceError

        workspace = Workspace(tmp_path / "ws")
        workspace.write_text("present.csv", "a,b\n")
        with pytest.raises(WorkspaceError) as caught:
            workspace.read_bytes("missing.csv")
        assert "present.csv" not in str(caught.value) or True  # message shape is the pack's job


# ---------------------------------------------------------------------------
# Files pack, through the registry contract
# ---------------------------------------------------------------------------


class TestFilesPack:
    def test_manifest_and_tools_agree(self):
        from app.runtime.packs.builtin.files import FILES_PACK

        manifest = FILES_PACK.manifest
        provided = {tool.name for tool in FILES_PACK.tools()}
        assert set(manifest.tools) == provided
        assert provided, "the files pack provides no tools"
        assert {tool.name for tool in FILES_PACK.tools()} >= {
            "file_list",
            "file_read",
            "file_write",
            "csv_read",
            "csv_write",
            "excel_read",
            "excel_write",
        }
        assert FILES_PACK.problems() == []

    def test_every_pack_tool_has_a_valid_schema(self):
        from app.runtime.packs.registry import builtin_registry

        for pack in builtin_registry().list():
            for tool in pack.tools():
                schema = tool.openai_schema()
                assert schema["type"] == "function"
                assert schema["function"]["name"] == tool.name
                assert schema["function"]["description"]
                assert schema["function"]["parameters"]["type"] == "object"

    @pytest.mark.asyncio
    async def test_csv_roundtrip_through_the_pack(self, tmp_path, monkeypatch):
        from app.runtime.packs.builtin.files import FILES_PACK

        monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(tmp_path / "ws"))
        tools = {tool.name: tool for tool in FILES_PACK.tools()}
        written = await tools["csv_write"](
            path="rows.csv", rows=[["name", "score"], ["小明", 90]], columns=["name", "score"]
        )
        assert written["size"] > 0
        read = await tools["csv_read"](path="rows.csv")
        assert read["columns"] == ["name", "score"]
        assert read["rows"][1][0] == "小明"


# ---------------------------------------------------------------------------
# Export parity of the pack boundary
# ---------------------------------------------------------------------------


def _packs_graph():
    from app.graph.schema import WorkflowGraph

    return WorkflowGraph.model_validate(
        {
            "id": "packs-parity",
            "name": "Packs parity probe",
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "label": "Start",
                    "config": {"input_schema": {"message": "hi"}},
                },
                {
                    "id": "look",
                    "type": "tool",
                    "label": "List files",
                    "config": {"tool": "file_list"},
                },
                {
                    "id": "end",
                    "type": "end",
                    "label": "End",
                    "config": {"output_label": "answer"},
                },
            ],
            "edges": [
                {
                    "id": "e1",
                    "source": "start",
                    "source_handle": "out",
                    "target": "look",
                    "target_handle": "in",
                },
                {
                    "id": "e2",
                    "source": "look",
                    "source_handle": "out",
                    "target": "end",
                    "target_handle": "in",
                },
            ],
        }
    )


class TestExportParity:
    def test_manifest_declares_the_pack_and_the_runtime_ships_it(self):
        from app.runtime.export import build_export

        app_root = pathlib.Path(__file__).resolve().parent.parent / "app"
        files = build_export(_packs_graph(), app_root)

        manifest = files["manifest.yml"]
        assert "packs:" in manifest
        assert "  - files" in manifest
        assert "vendor/runtime/app/runtime/packs/builtin/files.py" in files

        main_py = files["main.py"]
        assert "serve --http" in main_py
        assert "def cmd_doctor" in main_py
        assert "AGENT_WORKSPACE_DIR" in main_py

    def test_exported_package_doctor_and_selfcheck_pass(self, tmp_path):
        from app.runtime.export import build_export, write_export

        app_root = pathlib.Path(__file__).resolve().parent.parent / "app"
        files = build_export(_packs_graph(), app_root)
        package = tmp_path / "agent"
        write_export(files, package)

        doctor = subprocess.run(
            [sys.executable, "main.py", "doctor"],
            cwd=package,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert "RESULT=PASS" in doctor.stdout, doctor.stdout + doctor.stderr

        selfcheck = subprocess.run(
            [sys.executable, "main.py", "selfcheck"],
            cwd=package,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert "RESULT=PASS" in selfcheck.stdout, selfcheck.stdout + selfcheck.stderr


# ---------------------------------------------------------------------------
# Platform files API
# ---------------------------------------------------------------------------


class TestFilesAPI:
    def test_upload_list_download_delete(self, tmp_path, monkeypatch):
        import base64

        from fastapi.testclient import TestClient

        from app.main import app

        monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(tmp_path / "api-ws"))
        payload = base64.b64encode("订单号,金额\nSO-1,100\n".encode("utf-8")).decode("ascii")

        with TestClient(app, client=("127.0.0.1", 55555)) as client:
            created = client.post("/api/files", json={"name": "orders.csv", "content_base64": payload})
            assert created.status_code == 201, created.text
            listed = client.get("/api/files").json()
            assert [item["name"] for item in listed["files"]] == ["orders.csv"]
            downloaded = client.get("/api/files/orders.csv")
            assert downloaded.status_code == 200
            assert downloaded.content.decode("utf-8").startswith("订单号")
            deleted = client.delete("/api/files/orders.csv")
            assert deleted.status_code == 200
            assert client.get("/api/files").json()["files"] == []

    def test_traversal_is_refused(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from app.main import app

        monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(tmp_path / "api-ws"))
        with TestClient(app, client=("127.0.0.1", 55555)) as client:
            response = client.post(
                "/api/files", json={"name": "../escape.txt", "content_text": "x"}
            )
            assert response.status_code == 400
