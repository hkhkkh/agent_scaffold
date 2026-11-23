"""Shared fixtures.

Tests default to the mock chat model so the suite is hermetic — no network, no
API spend, deterministic. Mark a test with ``@pytest.mark.slow`` to opt into a
real provider call.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Iterator

import pytest

# Force mock mode before app.settings is imported, so no test ever reaches out
# to a real endpoint by accident.
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = "https://api.openai.com/v1"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch) -> Iterator[None]:
    """Give every test its own data directory and a freshly created schema.

    The data directory is per-test so runs, transcripts, and the database never
    leak between tests; the settings cache and engine cache are both cleared
    because each is keyed on configuration that just changed.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    from app.db.session import create_all, dispose_engines
    from app.settings import get_settings

    get_settings.cache_clear()
    dispose_engines()

    # The runtime records runs and steps as it executes, so the schema has to
    # exist even in tests that do not care about persistence.
    create_all()

    yield

    get_settings.cache_clear()
    dispose_engines()


@pytest.fixture(autouse=True)
def _platform_run_store() -> Iterator[None]:
    """Register the platform's database-backed store for the life of one test.

    ``app/main.py`` registers it in its lifespan, and tests that build a
    ``RunManager`` directly never enter that lifespan. Without this,
    ``RUN_STORE=sqlite`` — the default, and what the canvas actually runs —
    reaches ``build_run_store`` with no factory and reports a configuration
    error: 27 tests failed the moment the registration moved out of the runtime.

    **Registered by importing, not by importing a registration module.** The
    import is inside the fixture on purpose. ``app.db.session`` pulls SQLModel
    in, and ``tests/test_run_store.py`` asserts that importing the *runtime*
    does not — a module-level import here would install SQLModel into
    ``sys.modules`` before that test ran and make it pass for the wrong reason.

    Clearing on teardown matters as much as setting: the factory dict is
    process-wide, so a leak would let a later test build a store it declared it
    could not have.
    """
    from app.db.run_store import SqliteRunStore
    from app.runtime.runs import set_database_run_store

    set_database_run_store(SqliteRunStore)
    try:
        yield
    finally:
        set_database_run_store(None)


@pytest.fixture(autouse=True)
def _platform_workflow_loader() -> Iterator[None]:
    """Give the runtime a way to read definitions, the way `main.py` does.

    ``RunManager.hydrate`` asks ``app.runtime.workflows`` for the definition it
    is rebuilding. The runtime has no idea that definitions live in a database —
    that is the layer boundary task 27a draws — so the platform registers a
    loader, the same shape as ``set_subgraph_provider``. The application does it
    at startup; a test that builds a
    ``RunManager`` directly never gets there and would report every saved
    workflow as "never saved", which is a confusing way to say "no loader".

    This is the test-side twin of the ``_load_workflow`` function in
    ``app/main.py``, and it exists for the same reason the checkpointer fixture
    does: the lifespan is not running.
    """
    from app.db.repository import WorkflowNotFound, get_workflow
    from app.runtime.workflows import WorkflowUnavailable, set_workflow_loader

    def _load_workflow(workflow_id: str):
        try:
            return get_workflow(workflow_id)
        except WorkflowNotFound as exc:
            raise WorkflowUnavailable(str(exc)) from exc

    set_workflow_loader(_load_workflow)
    try:
        yield
    finally:
        set_workflow_loader(None)


@pytest.fixture(autouse=True)
async def _checkpointer_provider(_isolated_env) -> AsyncIterator[None]:
    """Open the configured checkpointer provider for the life of one test.

    The application opens it in its lifespan, and closes it on shutdown. Tests
    that build a `RunManager` directly never enter that lifespan, so a provider
    whose saver only exists after `open()` would refuse to hand one out.

    That is not hypothetical. The first time durable checkpoints became the
    default, **62 tests failed on `CheckpointerConfigError`** — a failure with
    nothing to do with checkpoints, reported from every test that executed a
    graph. The measurement is what turned "flipping the default probably needs a
    fixture" into a precise blocker; this fixture is the fix.

    Depends on `_isolated_env` so that DATA_DIR already points at this test's
    temporary directory. Without the ordering, every test would open the real
    `data/checkpoints.db` and share state with every other test.

    Teardown closes the provider *this fixture opened*, not whatever is
    installed at the end: `tests/test_checkpoints.py` deliberately swaps in its
    own providers, and closing one of those would break the test's own cleanup.
    """
    from app.runtime.checkpoints import (
        build_checkpointer_provider,
        set_checkpointer_provider,
    )

    provider = build_checkpointer_provider()
    await provider.open()
    set_checkpointer_provider(provider)
    try:
        yield
    finally:
        set_checkpointer_provider(None)
        await provider.close()


@pytest.fixture
def settings():
    from app.settings import get_settings

    return get_settings()


@pytest.fixture
def simple_graph() -> dict[str, Any]:
    """A minimal Start -> Prompt -> End workflow, as raw JSON."""
    return {
        "id": "wf-test",
        "name": "Echo",
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "label": "Start",
                "description": "Accepts the user's question.",
                "position": {"x": 0, "y": 0},
                "config": {"input_schema": {"query": "hello"}},
            },
            {
                "id": "tpl",
                "type": "prompt",
                "label": "Build prompt",
                "description": "Wraps the question in instructions.",
                "position": {"x": 260, "y": 0},
                "config": {"template": "Answer this: {{ vars.start.out.query }}"},
            },
            {
                "id": "end",
                "type": "end",
                "label": "End",
                "position": {"x": 520, "y": 0},
                "config": {"output_label": "result"},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "out", "target": "tpl", "targetHandle": "in"},
            {"id": "e2", "source": "tpl", "sourceHandle": "out", "target": "end", "targetHandle": "in"},
        ],
    }
