"""The run store: where history goes, and the dependency it removes.

The reason this file exists at all is a dependency arrow. ``runtime/manager.py``
imported ``app.db`` — the run framework reaching for the platform's database —
which meant "run a graph" implied "install SQLModel, SQLAlchemy, and the session
stack". For the canvas that is merely untidy. For an exported agent it is
disqualifying: the agent is supposed to be a standalone program, and a program
that cannot import itself without the platform is not one.

So the first class here is the one that matters, and it is written to be
falsifiable: putting ``from app.db import Run`` back at the top of ``manager.py``
must turn it red. ``_guardprobe_capability.py`` performs exactly that mutation and
checks that it does.

The rest split into the three implementations and the two invariants that are easy
to lose without noticing:

* the payload handed to a store is **already redacted** — that assertion is here
  because moving persistence behind an interface is precisely the kind of change
  that can quietly stop masking a credential;
* a persisted summary and a live summary have **the same key set**, so a client
  cannot tell which one answered.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.graph.schema import RunCreateRequest, RunStatus, WorkflowGraph
from app.runtime.manager import RunManager
from app.runtime.runs import (
    MemoryRunStore,
    NullRunStore,
    RunStore,
    RunStoreConfigError,
    build_run_store,
    get_run_store,
    set_run_store,
)
from app.settings import Settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PACKAGE = BACKEND_ROOT / "app" / "runtime"

#: A value shaped like a live credential. Used to check that redaction runs on
#: the path to the store; the exact string does not matter, only that it is not
#: the mask and not obviously a placeholder.
SECRET = "sk-live-DO-NOT-PERSIST"

#: The platform packages that must not be reachable from the run framework.
FORBIDDEN_IN_RUNTIME = ("app.db", "sqlmodel", "sqlalchemy", "fastapi")


@pytest.fixture(autouse=True)
def _restore_store():
    """The store is process-wide, so every test here must put it back.

    Without this a store left installed leaks into every later test in the same
    process — which is how a suite starts passing for the wrong reason.
    """
    yield
    set_run_store(None)


def _settings(**overrides: Any) -> Settings:
    """Deterministic settings: never read the developer's `.env`."""
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# 1. The dependency this whole task exists to remove
# ---------------------------------------------------------------------------


def _module_level_platform_imports(path: Path) -> list[str]:
    """Imports of a platform package that execute when ``path`` is imported.

    Module level only. A lazy import inside a function body runs only if that
    function is called, which is the whole point of the one in
    ``build_run_store`` — so the distinction is not a technicality, it is the
    mechanism. Walking manually rather than with :func:`ast.walk` because the
    latter does not report parents, and "is this inside a function" is the only
    question being asked.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    def visit(node: ast.AST, inside_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit(child, True)
                continue
            if isinstance(child, ast.Import):
                if not inside_function:
                    found.extend(alias.name for alias in child.names)
                continue
            if isinstance(child, ast.ImportFrom):
                if not inside_function and child.module:
                    # `from app.db.models import x` -> record the full path so a
                    # prefix test sees `app.db.models` and not just `app.db`.
                    base = "." * child.level + child.module
                    found.extend(
                        f"{base}.{alias.name}" if alias.name != "*" else base
                        for alias in child.names
                    )
                    found.append(base)
                continue
            visit(child, inside_function)

    visit(tree, False)
    return found


class TestTheRuntimeDoesNotImportThePlatformDatabase:
    """The gate. This is what turns "the runtime is standalone" into a check."""

    def test_no_runtime_module_imports_the_platform_at_module_level(self):
        offenders: list[str] = []
        for path in sorted(RUNTIME_PACKAGE.rglob("*.py")):
            for name in _module_level_platform_imports(path):
                if any(
                    name == forbidden or name.startswith(f"{forbidden}.")
                    for forbidden in FORBIDDEN_IN_RUNTIME
                ):
                    offenders.append(f"{path.name}: {name}")
        assert not offenders, (
            "The run framework must not depend on the platform. These imports make "
            "`import app.runtime.manager` pull in the database stack, which is what "
            "stops an exported agent from being a standalone program. Inject a store "
            f"through `app.runtime.runs` instead. Found: {offenders}"
        )

    def test_importing_the_run_manager_pulls_in_no_platform_package(self):
        """The observable version of the test above.

        Static analysis can only see imports it knows to look for; this runs a
        real interpreter and reports what a real import actually loaded. A new
        transitive dependency would be invisible to the AST check and obvious
        here.
        """
        probe = (
            "import sys\n"
            f"sys.path.insert(0, {str(BACKEND_ROOT)!r})\n"
            "import app.runtime.manager\n"
            "for name in ('app.db', 'sqlmodel', 'sqlalchemy', 'fastapi'):\n"
            "    if name in sys.modules:\n"
            "        print('LOADED', name)\n"
            "print('DONE')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            cwd=str(BACKEND_ROOT),
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        loaded = [
            line.split()[1]
            for line in result.stdout.splitlines()
            if line.startswith("LOADED")
        ]
        assert not loaded, (
            f"Importing app.runtime.manager loaded {loaded}. The run framework has "
            "to stay importable without the platform."
        )

    def test_the_one_lazy_import_is_actually_lazy(self):
        """`build_run_store('null')` must not touch the database module.

        The module-level check above would still pass if someone turned the lazy
        import into an eager one, because the AST walk would see it inside a
        function. This is what pins the laziness itself.
        """
        probe = (
            "import sys\n"
            f"sys.path.insert(0, {str(BACKEND_ROOT)!r})\n"
            "from app.runtime.runs import build_run_store\n"
            "from app.settings import Settings\n"
            "store = build_run_store(Settings(_env_file=None, run_store='null'))\n"
            "print('store', store.name)\n"
            "print('sqlmodel', 'sqlmodel' in sys.modules)\n"
            "print('app.db', 'app.db' in sys.modules)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            cwd=str(BACKEND_ROOT),
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "store null" in result.stdout
        assert "sqlmodel False" in result.stdout
        assert "app.db False" in result.stdout


# ---------------------------------------------------------------------------
# 2. Which implementation gets built
# ---------------------------------------------------------------------------


class TestSelection:
    def test_the_default_is_the_database(self):
        """Unconfigured behaviour must not change. The canvas relies on it."""
        store = build_run_store(_settings())
        assert store.name == "sqlite"

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ("sqlite", "sqlite"),
            ("memory", "memory"),
            ("null", "null"),
            ("  NULL  ", "null"),
            ("Memory", "memory"),
        ],
    )
    def test_a_configured_name_selects_that_store(self, configured, expected):
        assert build_run_store(_settings(run_store=configured)).name == expected

    def test_an_empty_value_falls_back_to_the_default(self):
        """An unset environment variable arrives as `''`, not as absent."""
        assert build_run_store(_settings(run_store="")).name == "sqlite"

    def test_an_unknown_name_is_an_error_not_a_fallback(self):
        """A typo must not be answered with a working-looking default.

        Someone who writes `RUN_STORE=nul` because their environment has no
        database is asking for no history. Silently giving them SQLite means the
        misconfiguration surfaces as runs that die writing history they were told
        not to keep — much later, and much less clearly.
        """
        with pytest.raises(RunStoreConfigError) as excinfo:
            build_run_store(_settings(run_store="nonsense"))
        message = str(excinfo.value)
        assert "nonsense" in message
        # The message has to name the alternatives, or it is only half a fix.
        for name in ("sqlite", "memory", "null"):
            assert name in message

    def test_the_getter_uses_the_same_selection(self):
        set_run_store(None)
        assert get_run_store().name == "sqlite"


# ---------------------------------------------------------------------------
# 3. The stores themselves, driven by a real run
# ---------------------------------------------------------------------------


def _node(node_id: str, node_type: str, **config: Any) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "label": node_id, "config": config}


def _edge(source: str, target: str) -> dict[str, Any]:
    return {
        "id": f"{source}->{target}",
        "source": source,
        "sourceHandle": "out",
        "target": target,
        "targetHandle": "in",
    }


def _linear(workflow_id: str = "wf-store", count: int = 1) -> WorkflowGraph:
    """``start`` -> ``count`` prompt nodes -> ``end``.

    Prompt nodes because they are deterministic and hermetic: these tests are
    about what gets written, not about what a model answers.
    """
    ids = ["start", *(f"p{i}" for i in range(count)), "end"]
    nodes = [_node("start", "start", input_schema={})]
    nodes += [_node(f"p{i}", "prompt", template=f"step {i}") for i in range(count)]
    nodes.append(_node("end", "end", output_label="result"))
    return WorkflowGraph(
        id=workflow_id,
        nodes=nodes,
        edges=[_edge(a, b) for a, b in zip(ids, ids[1:])],
    )


async def _run(graph: WorkflowGraph, *, inputs: dict[str, Any] | None = None):
    """Create a graph through a fresh manager and run it to a terminal state."""
    manager = RunManager()
    context = await manager.create(
        graph.id,
        graph,
        RunCreateRequest(
            workflow_id=graph.id, inputs=inputs or {}, recursion_limit=200
        ),
    )
    await manager.start(context)
    assert context.task is not None
    await context.task
    return manager, context


class TestMemoryStore:
    async def test_a_finished_run_is_recorded(self):
        set_run_store(MemoryRunStore())
        _, context = await _run(_linear(count=2))

        assert context.status is RunStatus.SUCCEEDED
        store = get_run_store()
        assert store.exists(context.id)

        summary = store.summary(context.id)
        assert summary is not None
        assert summary["id"] == context.id
        assert summary["status"] == RunStatus.SUCCEEDED.value
        assert summary["workflow_id"] == "wf-store"
        assert summary["outputs"] == context.outputs

    async def test_steps_are_recorded_in_execution_order(self):
        set_run_store(MemoryRunStore())
        _, context = await _run(_linear(count=2))

        steps = get_run_store().steps(context.id)
        assert [step["node_id"] for step in steps] == ["start", "p0", "p1", "end"]

        # Ascending rather than equal to `[0, 1, 2, 3]`: the numbering starts at 1,
        # and pinning the literal would make this test fail on a change to the
        # counter that says nothing about ordering.
        indexes = [step["index"] for step in steps]
        assert indexes == sorted(indexes)
        assert len(set(indexes)) == len(indexes)

    async def test_an_unknown_run_answers_instead_of_raising(self):
        store = MemoryRunStore()
        assert store.exists("nope") is False
        assert store.summary("nope") is None
        assert store.steps("nope") == []
        assert store.history() == []

    async def test_history_is_newest_first_and_can_be_filtered(self):
        set_run_store(MemoryRunStore())
        store = get_run_store()
        _, first = await _run(_linear("wf-a"))
        _, second = await _run(_linear("wf-a"))
        _, other = await _run(_linear("wf-b"))

        everything = store.history()
        assert {row["id"] for row in everything} == {first.id, second.id, other.id}

        only_a = store.history(workflow_id="wf-a")
        assert {row["id"] for row in only_a} == {first.id, second.id}

        assert len(store.history(limit=1)) == 1


class TestNullStore:
    """What an exported agent gets. The run must work; nothing must be written."""

    async def test_a_run_completes_and_writes_nothing(self):
        set_run_store(NullRunStore())
        _, context = await _run(_linear())

        # The run itself is unaffected — that is the requirement. A store that
        # somehow broke execution would defeat the point of offering it.
        assert context.status is RunStatus.SUCCEEDED
        assert context.outputs["result"] == "step 0"

        # And nothing reached the database, which is the other half.
        from app.db.models import Run as RunRow
        from app.db.session import session_scope

        with session_scope() as session:
            assert session.get(RunRow, context.id) is None

    async def test_it_reports_nothing_rather_than_raising(self):
        store = NullRunStore()
        assert store.exists("anything") is False
        assert store.summary("anything") is None
        assert store.steps("anything") == []
        assert store.history() == []


class TestSqliteStore:
    """The default. Its behaviour must match what the code did before the seam."""

    async def test_a_finished_run_is_recorded(self):
        set_run_store(build_run_store(_settings()))
        _, context = await _run(_linear(count=2))

        store = get_run_store()
        assert store.name == "sqlite"
        assert store.exists(context.id)

        summary = store.summary(context.id)
        assert summary is not None
        assert summary["status"] == RunStatus.SUCCEEDED.value
        assert summary["duration_ms"] is not None and summary["duration_ms"] >= 0

        steps = store.steps(context.id)
        assert [step["node_id"] for step in steps] == ["start", "p0", "p1", "end"]

        history = store.history("wf-store")
        assert context.id in {row["id"] for row in history}

    async def test_an_unknown_run_answers_instead_of_raising(self):
        store = build_run_store(_settings())
        assert store.exists("nope") is False
        assert store.summary("nope") is None
        assert store.steps("nope") == []


class TestTheTwoSummaryShapesAgree:
    """A client must not be able to tell a live summary from a stored one.

    This is the invariant that two read paths would have broken. Before the seam,
    ``api/runs.py`` built the persisted shape by hand in a second place; add a
    field to one and not the other and nothing fails — the UI just sees a key
    appear and disappear depending on whether the run had been evicted yet.
    """

    async def test_a_stored_summary_has_exactly_the_live_key_set(self):
        from app.api.runs import _persisted_summary, _run_summary

        set_run_store(build_run_store(_settings()))
        _, context = await _run(_linear())

        live = _run_summary(context, include_steps=True)
        stored = _persisted_summary(context.id, include_steps=True)

        assert stored.keys() == live.keys(), (
            "The persisted summary and the live summary must expose the same keys. "
            f"Only in live: {sorted(live.keys() - stored.keys())}; "
            f"only in stored: {sorted(stored.keys() - live.keys())}"
        )

    async def test_the_step_shapes_agree_too(self):
        from app.api.runs import _tracked_step, _context_steps

        set_run_store(build_run_store(_settings()))
        _, context = await _run(_linear())

        steps = _context_steps(context)
        assert steps, "the run recorded no steps, so this test proves nothing"

        live = _tracked_step(steps[0])
        stored = get_run_store().steps(context.id)[0]
        assert stored.keys() == live.keys(), (
            f"only in live: {sorted(live.keys() - stored.keys())}; "
            f"only in stored: {sorted(stored.keys() - live.keys())}"
        )


class TestEveryDatetimeComesBackLabelled:
    """Same instant, same label. A naive datetime is a different value.

    SQLite has no datetime type, so SQLAlchemy stores one as text and parses it
    back without the offset. Every timestamp in this database is written as aware
    UTC, so it came back as the right instant with its label removed — which is
    not a cosmetic difference:

    * ``naive == aware`` is ``False``, so the same instant from two paths
      disagreed.
    * ``sorted([naive, aware])`` raises ``TypeError``.

    The key-set test above could not catch this, because the keys were right and
    the *values* were wrong in a way that only shows up when something compares
    them. It did show up: a run rebuilt after a restart carried a naive
    ``created_at`` into the manager's live-run map beside aware ones, and
    ``GET /runs`` sorts that map by ``created_at``. Measured before the fix,
    ``RunManager.list()`` raised ``TypeError`` with one rehydrated run present.

    These assertions are on the values, not the keys, for exactly that reason.
    """

    async def test_every_read_path_returns_an_aware_utc_datetime(self):
        """Discovered rather than listed, so a new timestamp is covered on arrival.

        Writing the field names out by hand would go stale the day someone adds
        one, and the failure mode here is silent — a naive value only misbehaves
        when something compares it. So the payloads are walked for anything that
        *is* a datetime.
        """
        from datetime import datetime, timezone

        set_run_store(build_run_store(_settings()))
        _, context = await _run(_linear(count=2))

        store = get_run_store()
        payloads = {
            "summary": store.summary(context.id),
            "load": store.load(context.id),
            "steps[0]": store.steps(context.id)[0],
            "steps[-1]": store.steps(context.id)[-1],
            "history": next(
                row for row in store.history("wf-store") if row["id"] == context.id
            ),
        }
        assert all(payload is not None for payload in payloads.values())

        observed: dict[str, datetime] = {}
        for where, payload in payloads.items():
            for key, value in (payload or {}).items():
                if isinstance(value, datetime):
                    observed[f"{where}.{key}"] = value

        # Vacuity guard: the walk must actually have found the timestamps, or a
        # payload that stopped carrying them would read as a pass.
        assert {"summary.created_at", "load.created_at", "history.created_at"} <= (
            observed.keys()
        ), f"the walk found fewer timestamps than expected: {sorted(observed)}"

        unlabelled = [name for name, value in observed.items() if value.tzinfo is None]
        assert not unlabelled, (
            "these came back from the database with no timezone: "
            f"{sorted(unlabelled)}. A naive datetime does not equal the aware one "
            "it was written from, and cannot be compared against one."
        )
        assert {value.tzinfo for value in observed.values()} == {timezone.utc}

    async def test_the_stored_instant_equals_the_live_one(self):
        """Being aware is not enough — it has to be the same moment.

        Attaching the wrong offset would satisfy the test above and shift every
        timestamp by hours.
        """
        set_run_store(build_run_store(_settings()))
        _, context = await _run(_linear())

        assert get_run_store().summary(context.id)["created_at"] == context.created_at
        assert get_run_store().load(context.id)["created_at"] == context.created_at

    async def test_the_column_declares_utc_rather_than_leaving_it_to_readers(self):
        """The fix belongs on the type, and this pins it there.

        Fixing it in each reader would be one copy of the same knowledge per read
        path. There is exactly one reason the offset disappears, and it belongs to
        the column — so a model field that goes back to a plain ``datetime``
        reintroduces the bug for every reader at once.
        """
        from app.db.models import UtcDateTime

        for model in ("Run", "RunStep", "Workflow"):
            module = __import__("app.db.models", fromlist=[model])
            table = getattr(module, model).__table__
            for column in table.columns:
                if isinstance(column.type, UtcDateTime):
                    continue
                assert not str(column.type).upper().startswith("DATETIME"), (
                    f"{model}.{column.name} is a plain DATETIME, so it will hand "
                    "back a naive datetime to every reader"
                )


# ---------------------------------------------------------------------------
# 4. The seam is not cosmetic
# ---------------------------------------------------------------------------


class _RecordingStore(NullRunStore):
    """A null store that keeps what it was handed, so a test can inspect it."""

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.finishes: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    def start(self, run: dict[str, Any]) -> None:
        self.starts.append(run)

    def finish(self, run: dict[str, Any], steps: list[dict[str, Any]]) -> None:
        self.finishes.append((run, steps))


class TestTheInjectionPointIsReal:
    """Swapping the store must change what happens, or the interface is decoration.

    This is the failure mode the whole capability layer exists to avoid: a control
    that is correct, tested, and not actually installed. The same class of mistake
    was found in Phase 0, where the auth middleware passed every test because each
    test built its own app object.
    """

    async def test_the_manager_calls_the_installed_store(self):
        store = _RecordingStore()
        set_run_store(store)
        _, context = await _run(_linear(count=1))

        assert len(store.starts) == 1, "`_persist_start` did not reach the store"
        assert store.starts[0]["id"] == context.id
        assert store.starts[0]["workflow_id"] == "wf-store"

        assert len(store.finishes) == 1, "`_persist_finish` did not reach the store"
        run, steps = store.finishes[0]
        assert run["id"] == context.id
        assert run["status"] == RunStatus.SUCCEEDED.value
        assert [step["node_id"] for step in steps] == ["start", "p0", "end"]

    async def test_persistence_failure_does_not_abort_the_run(self):
        """The `except Exception` around persistence must survive the refactor.

        A store that throws is a broken database, and a broken database is not a
        reason to lose a run that already produced an answer.
        """

        class ExplodingStore(NullRunStore):
            def start(self, run):
                raise RuntimeError("database is on fire")

            def finish(self, run, steps):
                raise RuntimeError("database is still on fire")

        set_run_store(ExplodingStore())
        _, context = await _run(_linear())

        assert context.status is RunStatus.SUCCEEDED
        assert context.outputs["result"] == "step 0"


# ---------------------------------------------------------------------------
# 5. The security boundary moved, so it must be pinned
# ---------------------------------------------------------------------------


class TestCredentialsAreRedactedBeforeTheStoreSeesThem:
    """Redaction has to happen in the manager, and stay there.

    The tempting interface is ``start(context)`` — hand the store the live object
    and let it pull what it needs. That version moves redaction into every store
    implementation, and these tests are what make the substitution fail loudly
    instead of shipping a credential to disk.

    What ``redact`` actually does, because it decides what can be tested: it masks
    the **value under a secret-looking key**, walking nested dicts and lists
    (``SECRET_HINTS`` in ``runtime/serialize.py``). It is not a content scrubber,
    so a credential pasted into a prompt *string* survives it — a prompt node that
    templates a key into its output keeps the key. That limitation is real, it
    predates this task, and it is not something a store refactor should pretend to
    solve; what follows tests the part that is genuinely key-driven.
    """

    async def test_a_nested_credential_is_masked_in_the_stored_steps(self):
        """The regression this pins was found here, not imagined.

        ``api/runs._tracked_step`` redacts a live run's steps. ``_persist_finish``
        did not redact the payloads it wrote. Nothing failed, because no test
        compared the two — the visible effect was that a run showed a masked
        ``api_key`` for its first minute and a raw one after eviction, when the
        UI read the same step back from the store.

        The run's ``start`` node carries the inputs as its output, so the
        credential appears under its real key name inside a step payload.
        """
        store = _RecordingStore()
        set_run_store(store)

        await _run(_linear(), inputs={"api_key": SECRET, "query": "hello"})

        assert store.finishes, "nothing was handed to the store"
        _, steps = store.finishes[0]
        start = next((step for step in steps if step["node_id"] == "start"), None)
        assert start is not None, "the start node produced no step"

        dumped = repr(start)
        assert SECRET not in dumped, (
            "the raw credential reached a persisted step payload. A live run masks "
            "this same step, so the value would become visible the moment the run "
            "was evicted and read back from the store."
        )
        assert "hello" in dumped, "redaction took out a field it should not have"

    async def test_the_live_and_stored_step_payloads_agree_on_that_value(self):
        """Both read paths must mask it, not just one.

        Asserting on the stored side alone would still pass if someone deleted
        redaction from the live side instead. Comparing the two is what makes
        "they agree" the thing under test.
        """
        from app.api.runs import _context_steps, _tracked_step

        manager, context = await _run(
            _linear(), inputs={"api_key": SECRET, "query": "hello"}
        )
        del manager

        live = {step["node_id"]: step for step in map(_tracked_step, _context_steps(context))}
        stored = {step["node_id"]: step for step in get_run_store().steps(context.id)}

        assert live.keys() == stored.keys()
        for node_id in live:
            assert repr(live[node_id]) == repr(stored[node_id]), (
                f"the live and stored payloads for '{node_id}' differ, so the UI "
                "would show one thing before eviction and another after"
            )
        assert SECRET not in repr(live), f"the live view leaked the credential"
