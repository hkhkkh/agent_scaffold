"""The layer boundary: layer 2 must not name layer 1.

This is the gate from `target/delivery.md` §5 step ⑤ and `target/vision.md` §5
constraint 3 — the one that turns "every agent shares one foundation" from a
promise into a check that fails.

What it protects
----------------
Task 27a extracts the runtime into its own wheel. That copy is **source-level**:
`runtime_files()` reads the modules listed in `RUNTIME_PATHS` and writes them
into the agent package. A module there that says ``from app.db import ...``
imports a package that does not exist inside an exported agent, and the failure
appears on a customer's machine as ``ModuleNotFoundError: app.db``.

A lazy import is not a loophole, and this file exists because it was being used
as one. ``RunManager.hydrate`` had ``from app.db.repository import ...`` **inside
the method body**, so it never ran at import time and passed every check that
only walked module-level imports (``tests/test_run_store.py``). The module still
*travelled* into the exported package and still named a package that was not
there. Copying source does not care whether a line is lazy — so the check cannot
either.

Why it walks the sources rather than importing them
---------------------------------------------------
Importing a module proves it loads *here*, where the platform is installed. The
question is whether it would load *there*, where it is not — and only the source
can answer that, because the source is the thing that gets copied.

Reverse-validated
-----------------
``_guardprobe_capability.py`` puts the ``from app.db`` line back in
``manager.py`` and asserts this file goes red. A gate that cannot fail is
decoration.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import textwrap

import pytest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.runtime.export import RUNTIME_PATHS  # noqa: E402

APP_ROOT = BACKEND_ROOT / "app"

#: Modules that belong to the platform (layer 1). A runtime module may not
#: import any of them, at any nesting depth.
#:
#: Kept as module prefixes rather than exact names so ``app.db.models`` and
#: ``app.db`` are both caught, and so a new submodule is covered by default —
#: a list that has to be extended per submodule is a list that goes stale.
FORBIDDEN_PREFIXES = (
    "app.api",
    "app.db",
    "app.main",
    "fastapi",
    "sqlmodel",
    "sqlalchemy",
    "alembic",
    "starlette",
)


def _imports_anywhere(tree: ast.AST) -> list[str]:
    """Every module named by an import in ``tree``, at any depth.

    Deliberately every depth — module level, inside a function, inside a
    ``try``/``if TYPE_CHECKING`` block. The distinction the other tests draw
    (lazy vs eager) is about *when* a line runs. This check is about whether the
    line is there at all, because the line is what gets copied.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.append("." * node.level + node.module)
    return found


def _is_forbidden(module: str) -> str | None:
    for prefix in FORBIDDEN_PREFIXES:
        if module == prefix or module.startswith(f"{prefix}."):
            return prefix
    return None


def test_no_runtime_module_names_the_platform():
    """The gate. Every module that ships in the package, at every depth."""
    offenders: list[str] = []
    for rel in sorted(RUNTIME_PATHS):
        path = APP_ROOT / rel
        assert path.exists(), f"RUNTIME_PATHS lists '{rel}', which does not exist"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imports_anywhere(tree):
            hit = _is_forbidden(module)
            if hit:
                offenders.append(f"{rel}: imports '{module}'")

    assert not offenders, (
        "A module that ships inside an exported agent imports the platform. The "
        "package is a source-level copy, so the line travels even when it is lazy "
        "and even when the import never runs here. Inject a provider through "
        "`app.runtime.*` instead:\n  " + "\n  ".join(offenders)
    )


def test_the_forbidden_list_would_catch_a_real_offence():
    """Guard the guard: a typo in the prefixes must not make this file vacuous.

    Without this, misspelling ``app.db`` as ``app.dbx`` turns the gate green and
    nothing says so — which is precisely the failure mode the gate exists to
    prevent, one level up.
    """
    for prefix in FORBIDDEN_PREFIXES:
        assert _is_forbidden(prefix) == prefix, prefix
        assert _is_forbidden(f"{prefix}.anything") == prefix, prefix
    assert _is_forbidden("app.graph.compiler") is None
    assert _is_forbidden("langgraph.checkpoint") is None


def test_the_runtime_imports_without_a_workflow_loader():
    """The other half of the seam: no loader installed must not be a crash.

    An exported agent runs a graph handed to it directly and never rebuilds one,
    so "nothing installed" is its normal state. It must answer that plainly
    rather than raising an ImportError from inside the manager.
    """
    from app.runtime.workflows import (
        WorkflowUnavailable,
        has_workflow_loader,
        load_workflow,
        set_workflow_loader,
    )

    set_workflow_loader(None)
    try:
        assert has_workflow_loader() is False
        with pytest.raises(WorkflowUnavailable):
            load_workflow("wf-anything")
    finally:
        set_workflow_loader(None)


def test_hydrate_reports_an_unknown_workflow_as_a_run_error():
    """``hydrate`` catches the interface's exception, not the platform's.

    This is the behaviour the refactor had to preserve exactly: resuming a run
    whose definition was never saved still produces the same ``RunError`` with
    the same explanation, now sourced from the injected loader instead of a direct
    ``app.db`` call.
    """
    import asyncio

    from app.runtime.manager import RunError, RunManager
    from app.runtime.runs import MemoryRunStore, set_run_store

    class _StubStore(MemoryRunStore):
        def load(self, run_id: str):  # noqa: D102 - test double
            return {
                "id": run_id,
                "workflow_id": "wf-never-saved",
                "status": "interrupted",
                "inputs": {},
                "thread_id": run_id,
                "tweaks": {},
                "recursion_limit": 50,
                "created_at": None,
            }

    set_run_store(_StubStore())
    try:
        with pytest.raises(RunError) as excinfo:
            asyncio.run(RunManager().hydrate("r-1"))
    finally:
        set_run_store(None)

    assert "never saved" in str(excinfo.value)


def test_the_vendored_package_carries_no_platform_reference():
    """The end-to-end version: parse the runtime copy the exporter actually writes.

    ``test_no_runtime_module_names_the_platform`` checks the *list* against the
    repository. This checks the *bytes that ship*, so a module copied by some
    other path, or a template that embeds an import, is caught too.

    **Parsed, not grepped.** The first version of this test used a regex over the
    file text and flagged ``runtime/workflows.py`` — whose docstring *explains*
    that it does not import ``app.db``. A gate that fails on the comment
    describing the rule is not stricter, it is wrong, and the fix someone
    reaches for is deleting the explanation. ``ast`` sees code and nothing else.

    Every depth, not just module level: the export is a source copy, so the
    lazy-import distinction ``tests/test_run_store.py`` draws deliberately does
    not apply here. ``_imports_anywhere`` above is that walk; this reuses it
    rather than growing a second one.
    """
    from app.graph.schema import WorkflowGraph
    from app.runtime.export import build_export

    graph = WorkflowGraph(id="wf-gate", name="gate", nodes=[], edges=[])
    files = build_export(graph, APP_ROOT)

    offenders: list[str] = []
    for name, content in sorted(files.items()):
        if not (name.startswith("vendor/runtime/") and name.endswith(".py")):
            continue
        try:
            tree = ast.parse(content, filename=name)
        except SyntaxError as exc:  # a runtime module that will not parse
            offenders.append(f"{name}: cannot be parsed ({exc})")
            continue
        for module in _imports_anywhere(tree):
            if _is_forbidden(module):
                offenders.append(f"{name}: imports '{module}'")

    assert not offenders, (
        "The exported package contains a runtime module that imports the "
        "platform. The package is a source-level copy, so the import travels:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# The seam convention: one shape, not a shape per author
# ---------------------------------------------------------------------------

DEFINITION_SEAMS = (
    "graph/subgraphs.py",
    "runtime/workflows.py",
)

FORBIDDEN_SEAM_SHAPES = ("ABC", "Provider")


def _seam_class_offenders(source: str) -> list[str]:
    """Class names in ``source`` that mean a seam has grown a class interface."""
    tree = ast.parse(source)
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(word in node.name for word in FORBIDDEN_SEAM_SHAPES)
    ]


@pytest.mark.parametrize("rel", DEFINITION_SEAMS)
def test_a_definition_seam_uses_the_function_shape(rel: str):
    """Each seam publishes set_* + has_* and defines no seam class."""
    source = (APP_ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=rel)
    functions = {
        n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    offenders = _seam_class_offenders(source)
    assert not offenders, (
        f"{rel} defines {offenders}. A definition seam is a module-level callable, "
        "not a class hierarchy - copy app/graph/subgraphs.py. Two conventions for "
        "one problem is the cost this test exists to avoid."
    )
    assert any(name.startswith("set_") for name in functions), f"{rel} has no set_* injector"
    assert any(name.startswith("has_") for name in functions), (
        f"{rel} has no has_* probe, so a caller cannot ask whether the platform "
        "installed anything"
    )


def test_the_seam_convention_would_catch_the_class_version():
    """Reverse validation: the shape this forbids must actually trip it."""
    the_version_i_wrote = textwrap.dedent(
        """
        from abc import ABC, abstractmethod

        class WorkflowProvider(ABC):
            @abstractmethod
            def get(self, workflow_id: str): ...

        class NullWorkflowProvider(WorkflowProvider):
            def get(self, workflow_id: str): ...

        def get_workflow_provider(): ...
        def set_workflow_provider(provider): ...
        """
    )
    offenders = _seam_class_offenders(the_version_i_wrote)
    assert offenders == ["WorkflowProvider", "NullWorkflowProvider"], (
        "the forbidden-shape detector does not recognise the version that "
        f"motivated it; it found {offenders}"
    )
    functions = {
        n.name
        for n in ast.parse(the_version_i_wrote).body
        if isinstance(n, ast.FunctionDef)
    }
    assert not any(name.startswith("has_") for name in functions), (
        "that version also lacked a has_* probe, which is the other half of the "
        "shape this test pins"
    )
