"""Export an agent package and run it as a separate process.

The export is only worth anything if the result runs. Everything here goes through
`subprocess`, because an in-process test would pass against the platform's own
imports — the exact thing an exported agent does not have.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile

import yaml

os.environ.setdefault("LLM_API_KEY", "")

from app.graph.schema import WorkflowGraph  # noqa: E402
from app.runtime.export import (  # noqa: E402
    REQUIREMENTS_LOCK_NAME,
    RUNTIME_PATHS,
    build_export,
    runtime_files,
    write_export,
)

APP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"
PYTHON = sys.executable

SECRET = "sk-live-DO-NOT-SHIP"


def _node(node_id: str, node_type: str, **config):
    return {"id": node_id, "type": node_type, "label": node_id, "config": config}


def _edge(source: str, target: str):
    return {
        "id": f"{source}->{target}",
        "source": source,
        "sourceHandle": "out",
        "target": target,
        "targetHandle": "in",
    }


def _graph() -> WorkflowGraph:
    """Two prompt nodes, so the run produces a value derived from its input."""
    return WorkflowGraph(
        id="wf-export",
        name="Export probe",
        nodes=[
            _node("start", "start", input_schema={"query": "hello"}),
            _node("a", "prompt", template="A[{{ vars.start.out.query }}]"),
            _node("b", "prompt", template="B[{{ vars.a.out }}]"),
            _node("end", "end", output_label="result"),
        ],
        edges=[_edge("start", "a"), _edge("a", "b"), _edge("b", "end")],
    )


def _tool_graph(tool: str = "current_time") -> WorkflowGraph:
    """A graph that proves the package keeps the runtime's built-in tools.

    An earlier exporter replaced the registry with the agent's own (empty) tool
    directory, so `Tool` nodes failed with "unknown tool" even for tools that
    ship with the runtime. This graph is the regression pin.
    """
    return WorkflowGraph(
        id="wf-export-tool",
        name="Export tool probe",
        nodes=[
            _node("start", "start", input_schema={"query": "hello"}),
            _node("t", "tool", tool=tool, arguments={"timezone_offset_hours": 8}),
            _node("end", "end", output_label="result"),
        ],
        edges=[_edge("start", "t"), _edge("t", "end")],
    )


def _export_graph(graph: WorkflowGraph, target: pathlib.Path) -> dict:
    files = build_export(graph, APP_ROOT, runtime_version="9.9.9")
    result = write_export(files, target)
    return {"files": files, "result": result}


def _export(target: pathlib.Path) -> dict:
    return _export_graph(_graph(), target)


def test_the_export_contains_every_runtime_module():
    files = build_export(_graph(), APP_ROOT)
    declared = [
        name
        for name in files
        if name.startswith("vendor/runtime/app/") and name.endswith(".py")
    ]
    assert len(declared) == len(runtime_files(APP_ROOT)), (
        "the vendored runtime must contain exactly what RUNTIME_PATHS declares"
    )
    for required in ("main.py", "agent.yaml", "agent.lock", "requirements.txt", "Dockerfile"):
        assert required in files, f"{required} is missing from the package"


def test_every_module_the_runtime_imports_is_declared():
    """The list is hand-maintained, so it needs a check rather than care.

    Twice now a new package has been added and `RUNTIME_PATHS` not updated, and
    both times the symptom was the *export tests* failing, not the feature: the
    import is at module level, so an agent package does not degrade, it refuses to
    start. This walks the modules the manifest declares and follows their imports,
    so an omission fails here — next to the list — with a message naming what to
    add.

    **Only module-level imports count.** An import inside a function body is
    reached on demand, and two of them are deliberate: `app.db.*` is the platform
    and an exported agent has no database, and `app.runtime.ai.messages` is imported
    lazily to keep an import cycle out of module load. Flagging those would make
    this test demand that an exported agent contain the platform.
    """
    import ast

    declared = set(RUNTIME_PATHS)
    declared_dirs = {name.rsplit("/", 1)[0] for name in declared if "/" in name}

    missing: dict[str, set[str]] = {}
    for rel in sorted(declared):
        path = APP_ROOT / rel
        if not path.exists():
            missing.setdefault("<missing file>", set()).add(rel)
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _module_level_nodes(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module
                if node.level == 0 and module and module.startswith("app."):
                    _check_import(module, declared, declared_dirs, missing)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app."):
                        _check_import(alias.name, declared, declared_dirs, missing)

    assert not missing, (
        "RUNTIME_PATHS is missing modules that the runtime imports at module "
        "level. An exported agent fails to start without them:\n"
        + "\n".join(f"  {rel} -> {sorted(names)}" for rel, names in sorted(missing.items()))
    )


def _module_level_nodes(tree):
    """Every `import` at module level, including inside `if TYPE_CHECKING`.

    Descends into `if` / `try` bodies but **not** into `def` or `class`: a
    module-level import behind a flag still runs at import time in at least one
    configuration, while one inside a function does not run until called.
    """
    import ast

    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, (ast.If, ast.Try)):
            stack.extend(node.body)
            stack.extend(getattr(node, "orelse", []) or [])
            stack.extend(getattr(node, "finalbody", []) or [])
            for handler in getattr(node, "handlers", []) or []:
                stack.extend(handler.body)


def _check_import(
    module: str, declared: set[str], declared_dirs: set[str], missing: dict[str, set[str]]
) -> None:
    """Record a runtime module that the manifest does not declare."""
    rel = module[len("app.") :].replace(".", "/")
    candidates = [f"{rel}.py", f"{rel}/__init__.py"]
    if any(candidate in declared for candidate in candidates):
        return
    # A name that resolves to a declared *package* is fine: importing
    # `app.memory` finds `memory/__init__.py`, which RUNTIME_PATHS lists.
    if rel in declared_dirs and f"{rel}/__init__.py" in declared:
        return
    # Only record it if the module exists in this tree at all. A third-party
    # `app.*` cannot happen, but a *typo'd* import can, and that is a different
    # failure with a different fix.
    #
    # `APP_ROOT` **is** `app/`, so the candidates are relative to it. Getting this
    # wrong made the whole test pass vacuously: the file was never found, so
    # nothing was ever recorded as missing. Caught by reverse validation, not by
    # reading it.
    for candidate in candidates:
        if (APP_ROOT / candidate).exists():
            missing.setdefault(rel, set()).add(module)
            return


def test_every_package_the_export_imports_is_in_the_lock():
    """`RUNTIME_PATHS` covers our own modules; this covers everyone else's.

    The test above follows `app.*` imports, so it says nothing about third-party
    packages — those are declared somewhere else entirely, resolved by
    `scripts/build_vendor.py`. That left a hole exactly the shape of a lazy
    import: `runtime/checkpoints.py` reaches `aiosqlite` and
    `langgraph.checkpoint.sqlite.aio` from inside a method, so no module-level
    import pulled them into the closure, and the omission was invisible until an
    exported agent was run with `CHECKPOINT_STORE=sqlite`. The export built, the
    seal verified, `selfcheck` passed, and the agent still died on first use.

    **Every import counts, including ones inside functions** — the opposite of
    the rule above, and deliberately so. An `app.*` import inside a function is
    the platform, which an agent is meant not to have; a third-party import
    inside a function is a package the agent still has to ship, because the code
    path is reachable.
    """
    import ast

    files = build_export(_graph(), APP_ROOT)

    imported: dict[str, set[str]] = {}
    for name, content in files.items():
        if not name.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(content)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for module in names:
                top = module.split(".")[0]
                if not top or top in sys.stdlib_module_names or top.startswith("app"):
                    continue
                imported.setdefault(module, set()).add(name)

    lock = _lock_distributions()

    # The business connectors import these lazily, and only when the workflow
    # actually selects MySQL/PostgreSQL or a PDF. They are intentionally not in
    # the export's dependency closure (a portable agent should not carry three
    # database drivers it may never use); the nodes raise a clear "install X"
    # error instead. Everything else must stay pinned.
    optional_connector_modules = {"pymysql", "psycopg2", "pypdf"}

    missing: dict[str, list[str]] = {}
    for module in sorted(imported):
        if module.split(".")[0] in optional_connector_modules:
            continue
        unresolved = [
            dist
            for dist in _providing_distributions(module)
            if _normalise(dist) not in lock
        ]
        if unresolved:
            missing[module] = unresolved

    assert not missing, (
        "the export imports third-party packages that `_vendor_requirements.txt` "
        "does not pin. An agent built from it installs cleanly and then fails at "
        "run time:\n"
        + "\n".join(
            f"  {module} needs {', '.join(dists)}"
            f"   (imported by {', '.join(sorted(imported[module]))})"
            for module, dists in sorted(missing.items())
        )
        + "\nAdd the missing names to Direct in scripts/build_vendor.py, re-run "
        "it, and commit the regenerated lock alongside the wheels it wrote."
    )


@functools.lru_cache(maxsize=1)
def _shipped_paths() -> dict[str, str]:
    """Every installed file's path -> the distribution that ships it.

    Built once per session. Keyed case-insensitively because Windows will hand
    back `site-packages` and `Site-Packages` for the same directory depending on
    who is asking.
    """
    import importlib.metadata

    index: dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        for entry in dist.files or ():
            if ".dist-info" in entry.parts:
                continue
            try:
                path = dist.locate_file(entry)
            except Exception:  # noqa: BLE001 - a broken RECORD is not our problem
                continue
            index.setdefault(os.path.normcase(str(path)), name)
    return index


def _providing_distributions(module: str) -> list[str]:
    """The distribution(s) that own `module`, decided by where the file lives.

    Namespace packages are the reason this cannot stop at the top-level name.
    `langgraph.checkpoint.sqlite.aio` sits in the same `langgraph/` directory as
    `langgraph.graph`, but ships in a *different* wheel
    (`langgraph-checkpoint-sqlite`); a check that asked about `langgraph` would
    have reported it covered and missed the import that mattered.

    Falls back to the top-level name when the file cannot be located — an
    editable install leaves nothing in `RECORD` — so an unresolved module is
    reported rather than silently skipped.
    """
    import importlib.util

    index = _shipped_paths()
    found: set[str] = set()
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        spec = None
    if spec is not None:
        candidates = []
        if spec.origin and spec.origin.endswith(".py"):
            candidates.append(spec.origin)
        for directory in spec.submodule_search_locations or ():
            candidates.append(str(pathlib.Path(directory) / "__init__.py"))
        for candidate in candidates:
            owner = index.get(os.path.normcase(candidate))
            if owner:
                found.add(owner)
    return sorted(found) or [module.split(".")[0]]


def _normalise(distribution: str) -> str:
    """PEP 503 name normalisation, so `typing_extensions` matches the lock."""
    return re.sub(r"[-_.]+", "-", distribution).lower()


def _lock_distributions() -> set[str]:
    """The names pinned in the vendor lock, normalised.

    Read through the exporter's own constant rather than a path written here, so
    the test cannot disagree with the code about where the lock lives.
    """
    lock = APP_ROOT.parent / REQUIREMENTS_LOCK_NAME
    assert lock.exists(), (
        f"{lock} is missing. It is tracked in git and is what the sealed export "
        "installs from; without it the export silently degrades to unsealed."
    )
    names = set()
    for line in lock.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(_normalise(re.split(r"[=<>!~\[]", line)[0].strip()))
    return names


def test_credentials_are_not_in_the_export():
    """The one non-negotiable: shipping a key is shipping a leak."""
    graph = _graph()
    graph.nodes[1].config["api_key"] = SECRET
    files = build_export(graph, APP_ROOT)

    for name, content in files.items():
        assert SECRET not in content, f"the credential reached {name}"

    lock = json.loads(files["agent.lock"])
    assert lock["redacted_credentials"], "the export did not report what it stripped"
    assert "a_api_key" in files["config/.env.example"].lower(), (
        "the credential has no placeholder for a deployer to fill in"
    )


def test_the_lock_hash_matches_the_written_definition():
    """A lock file that disagrees with its own file is worse than none."""
    import hashlib

    files = build_export(_graph(), APP_ROOT)
    lock = json.loads(files["agent.lock"])
    actual = hashlib.sha256(files["agent.yaml"].encode("utf-8")).hexdigest()[:16]
    assert lock["graph_hash"] == actual


def _clean_env() -> dict[str, str]:
    """A subprocess environment with no path back to this repository.

    `-I` would be the tidier isolation flag, but on this machine it fails during
    interpreter startup (`OSError: [WinError 10106]` from `import _overlapped`),
    which has nothing to do with the agent. Clearing `PYTHONPATH` and dropping
    `PYTHONHOME` gets the property that matters — the package cannot import the
    platform even if it tried.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "LLM_API_KEY": "",
        "LLM_BASE_URL": "https://api.openai.com/v1",
    }
    return env


class TestTheExportedAgentActuallyRuns:
    def test_selfcheck_passes_on_a_fresh_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "agent"
            _export(root)

            result = subprocess.run(
                [PYTHON, "main.py", "selfcheck"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=180,
                env=_clean_env(),
            )
            assert "RESULT=PASS" in result.stdout, (
                f"selfcheck failed:\n{result.stdout}\n{result.stderr}"
            )

    def test_the_agent_runs_and_produces_the_expected_output(self):
        """The headline claim: a separate process, no platform, real output.

        The environment is emptied of anything that could resolve the platform's
        own modules, so a hidden dependency on them would fail here rather than on
        a customer's machine.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "agent"
            _export(root)

            result = subprocess.run(
                [PYTHON, "main.py", "run", "--input", json.dumps({"query": "world"})],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=180,
                env=_clean_env(),
            )
            assert result.returncode == 0, (
                f"the exported agent failed to run:\n{result.stdout}\n{result.stderr}"
            )
            output = json.loads(result.stdout)
            assert output["result"] == "B[A[world]]", output

    def test_the_exported_agent_keeps_runtime_tools(self):
        """`Tool` nodes must see built-ins, not only the package's own tools."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "agent"
            _export_graph(_tool_graph(), root)

            result = subprocess.run(
                [PYTHON, "main.py", "run", "--input", json.dumps({"query": "world"})],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=180,
                env=_clean_env(),
            )
            assert result.returncode == 0, result.stdout + result.stderr
            output = json.loads(result.stdout)
            assert output["result"]["iso"], output

    def test_daemon_runs_bounded_cycles_and_writes_health(self):
        """The continuity primitive: two ticks, two rows, one health file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "agent"
            _export_graph(_tool_graph(), root)

            result = subprocess.run(
                [
                    PYTHON,
                    "main.py",
                    "daemon",
                    "--cycles",
                    "2",
                    "--interval",
                    "1",
                    "--input",
                    json.dumps({"query": "world"}),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=180,
                env=_clean_env(),
            )
            assert result.returncode == 0, result.stdout + result.stderr

            db = sqlite3.connect(root / "data" / "agent_cycles.db")
            try:
                rows = db.execute(
                    "SELECT mode, status, output FROM cycles ORDER BY started_at"
                ).fetchall()
            finally:
                db.close()
            assert [(row[0], row[1]) for row in rows] == [
                ("monitor", "succeeded"),
                ("monitor", "succeeded"),
            ]
            assert '"iso"' in rows[0][2], rows[0][2]

            health = json.loads((root / "data" / "health.json").read_text("utf-8"))
            assert health["cycles_total"] == 2
            assert health["consecutive_failures"] == 0
            assert health["status"] == "succeeded"

    def test_daemon_reloads_custom_tools_exactly_once(self):
        """A second tick must not re-register the package's own tools.

        `prepare()` used to run on every cycle; once the built-in registry was
        kept, a custom tool registered on tick 1 made tick 2 fail with
        "Duplicate tool name". This pins the once-per-process fix.
        """
        code = '''from app.tools.registry import ToolDef, register_tool


async def _ping():
    return {"pong": True}


register_tool(
    ToolDef(
        name="ping",
        label="Ping",
        description="Return pong.",
        parameters={"type": "object", "properties": {}},
        fn=_ping,
    )
)
'''
        graph = WorkflowGraph(
            id="wf-export-custom-tool",
            name="Custom tool probe",
            nodes=[
                _node("start", "start", input_schema={}),
                _node("t", "tool", tool="ping", arguments={}),
                _node("end", "end", output_label="result"),
            ],
            edges=[_edge("start", "t"), _edge("t", "end")],
            tools={"ping": code},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "agent"
            _export_graph(graph, root)

            result = subprocess.run(
                [PYTHON, "main.py", "daemon", "--cycles", "2", "--interval", "1"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=180,
                env=_clean_env(),
            )
            assert result.returncode == 0, result.stdout + result.stderr

            db = sqlite3.connect(root / "data" / "agent_cycles.db")
            try:
                statuses = [
                    row[0]
                    for row in db.execute("SELECT status FROM cycles ORDER BY started_at")
                ]
            finally:
                db.close()
            assert statuses == ["succeeded", "succeeded"], statuses

    def test_a_failed_cycle_is_recorded_and_does_not_stop_the_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "agent"
            _export_graph(_tool_graph("no_such_tool"), root)

            result = subprocess.run(
                [
                    PYTHON,
                    "main.py",
                    "daemon",
                    "--cycles",
                    "2",
                    "--interval",
                    "1",
                    "--input",
                    json.dumps({"query": "world"}),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=180,
                env=_clean_env(),
            )
            # The process exits 0: it did what it was told and recorded the
            # failures. A supervisor should not restart it just because a cycle
            # failed.
            assert result.returncode == 0, result.stdout + result.stderr

            db = sqlite3.connect(root / "data" / "agent_cycles.db")
            try:
                statuses = [
                    row[0]
                    for row in db.execute("SELECT status FROM cycles ORDER BY started_at")
                ]
                failures = db.execute(
                    "SELECT value FROM state WHERE key='consecutive_failures'"
                ).fetchone()[0]
            finally:
                db.close()
            assert statuses == ["failed", "failed"]
            assert failures == "2"

    def test_daily_report_runs_once_per_day(self):
        """The marker lives on disk, so a second tick cannot resend the report."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "agent"
            _export(root)

            result = subprocess.run(
                [
                    PYTHON,
                    "main.py",
                    "daemon",
                    "--cycles",
                    "2",
                    "--interval",
                    "1",
                    "--input",
                    json.dumps({"query": "monitor"}),
                    # 00:00 is always due, which makes this independent of when
                    # the test runs without freezing the clock.
                    "--daily-at",
                    "00:00",
                    "--daily-input",
                    json.dumps({"query": "daily"}),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=180,
                env=_clean_env(),
            )
            assert result.returncode == 0, result.stdout + result.stderr

            db = sqlite3.connect(root / "data" / "agent_cycles.db")
            try:
                modes = [
                    row[0]
                    for row in db.execute("SELECT mode FROM cycles ORDER BY started_at")
                ]
            finally:
                db.close()
            assert modes == ["monitor", "daily", "monitor"], modes

    def test_daily_marker_survives_a_restart(self):
        """A supervisor restart the same day must not resend the report."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "agent"
            _export(root)

            command = [
                PYTHON,
                "main.py",
                "daemon",
                "--cycles",
                "1",
                "--interval",
                "1",
                "--input",
                json.dumps({"query": "monitor"}),
                "--daily-at",
                "00:00",
                "--daily-input",
                json.dumps({"query": "daily"}),
            ]
            first = subprocess.run(
                command, cwd=root, capture_output=True, text=True, timeout=180,
                env=_clean_env(),
            )
            second = subprocess.run(
                command, cwd=root, capture_output=True, text=True, timeout=180,
                env=_clean_env(),
            )
            assert first.returncode == 0, first.stdout + first.stderr
            assert second.returncode == 0, second.stdout + second.stderr

            db = sqlite3.connect(root / "data" / "agent_cycles.db")
            try:
                modes = [
                    row[0]
                    for row in db.execute("SELECT mode FROM cycles ORDER BY started_at")
                ]
            finally:
                db.close()
            assert modes.count("daily") == 1, modes

    def test_daemon_refuses_a_second_instance(self):
        """Two copies would double every write; the lock is the guard."""
        # The lock is exercised through the generated file, because the failure
        # mode is OS-specific (msvcrt on Windows, flock elsewhere).
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "agent"
            _export(root)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            lock_path = data_dir / "agent.daemon.lock"
            holder = open(lock_path, "a+")
            holder.write("0")
            holder.flush()
            holder.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                result = subprocess.run(
                    [
                        PYTHON,
                        "main.py",
                        "daemon",
                        "--cycles",
                        "1",
                        "--input",
                        "{}",
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=_clean_env(),
                )
            finally:
                holder.close()
            assert result.returncode == 1, result.stdout + result.stderr
            assert "another daemon" in result.stderr

    def test_selfcheck_notices_an_edited_runtime(self):
        """The seal has to fail when it should, or it is decoration."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "agent"
            _export(root)

            edited = root / "vendor" / "runtime" / "app" / "runtime" / "serialize.py"
            edited.write_text(
                edited.read_text(encoding="utf-8") + "\n# edited\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [PYTHON, "main.py", "selfcheck"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=180,
                env=_clean_env(),
            )
            assert "runtime changed" in result.stdout, result.stdout + result.stderr
            assert "RESULT=FAIL" in result.stdout

    def test_selfcheck_notices_an_edited_definition(self):
        """Same seal, the other half: editing the workflow is detected too."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "agent"
            _export(root)

            agent_yaml = root / "agent.yaml"
            # A loader that keeps `!ref` as its path string, so dumping the file
            # back writes the same reference rather than trying to resolve it.
            loader_cls = type("_L", (yaml.SafeLoader,), {})
            loader_cls.add_constructor(
                "!ref", lambda loader, node: loader.construct_scalar(node)
            )
            payload = yaml.load(agent_yaml.read_text(encoding="utf-8"), Loader=loader_cls)
            payload["nodes"][1]["config"]["template"] = "EDITED"
            agent_yaml.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            result = subprocess.run(
                [PYTHON, "main.py", "selfcheck"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=180,
                env=_clean_env(),
            )
            assert "agent.yaml changed" in result.stdout, result.stdout + result.stderr

    def test_the_agent_does_not_import_the_platform(self):
        """No `app.db`, no `app.api` — checked by running the import in isolation.

        Without the vendored runtime on the path these would simply fail, so the
        check is: the package's own runtime must not contain a module-level
        platform import. Function-level ones (the store builder, `hydrate`) are
        fine and unreachable for a package that uses `RUN_STORE=null`.
        """
        import ast

        offenders: list[str] = []
        for path in sorted((APP_ROOT).rglob("*.py")):
            rel = path.relative_to(APP_ROOT).as_posix()
            if not any(
                rel == entry or rel.startswith(entry.rsplit("/", 1)[0] + "/")
                for entry in runtime_files(APP_ROOT)
            ):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.db"):
                    offenders.append(f"{rel}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("app.db") or alias.name == "fastapi":
                            offenders.append(f"{rel}:{node.lineno}")
        assert not offenders, f"module-level platform imports: {offenders}"
