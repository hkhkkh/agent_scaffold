"""Memory through the HTTP API, across two runs.

Why this exists in addition to `test_memory.py`: every failure found while
building this feature was a *wiring* failure, not a logic failure. The node was
correct from the first run; what broke was the export manifest, the icon name, the
lifespan hookup. None of those are visible to a unit test that constructs the
store itself.

So this drives the app the way a browser does: save a graph, start a run, read the
result, start another run. The second run is the assertion that matters — it
recalls a fact the *first, separate run* wrote, which is the difference between
long-term memory and shared state.

The chat model is mocked. The embedding model is not: it is either the real
endpoint when a key is configured, or absent, and both are asserted to work.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app

#: The fact the first run writes and the second looks up. Chinese on purpose —
#: semantic retrieval is the thing under test, and an English-only test against a
#: Chinese-capable embedder proves less.
FACT = "本公司的订单号统一以 ACME- 开头"
QUESTION = "订单号是什么格式"


def _graph(workflow_id: str, namespace: str = "{agent}") -> dict:
    return {
        "id": workflow_id,
        "name": "Memory probe",
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "label": "Start",
                "config": {"input_schema": {"text": {"type": "string"}}},
            },
            {
                "id": "remember",
                "type": "memory",
                "label": "Remember",
                "config": {
                    "mode": "write",
                    "namespace": namespace,
                    "text": "{{ inputs.in.text }}",
                },
            },
            {
                "id": "recall",
                "type": "memory",
                "label": "Recall",
                "config": {
                    "mode": "search",
                    "namespace": namespace,
                    "query": "{{ inputs.in.text }}",
                    "limit": 3,
                },
            },
            {"id": "end", "type": "end", "label": "End", "config": {"output_label": "memory"}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "start",
                "sourceHandle": "out",
                "target": "remember",
                "targetHandle": "in",
            },
            {
                "id": "e2",
                "source": "remember",
                "sourceHandle": "out",
                "target": "recall",
                "targetHandle": "in",
            },
            {
                "id": "e3",
                "source": "recall",
                "sourceHandle": "out",
                "target": "end",
                "targetHandle": "in",
            },
        ],
    }


def _run_to_completion(client: TestClient, workflow_id: str, text: str) -> dict:
    """Start a run and wait for it, returning the run record.

    Polls rather than streams: a test that reads the SSE feed would be testing the
    event stream, which has its own tests.
    """
    response = client.post(
        "/api/runs", json={"workflow_id": workflow_id, "inputs": {"text": text}}
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = client.get(f"/api/runs/{run_id}").json()
        if state["status"] in ("succeeded", "failed", "cancelled"):
            return state
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish within 60s")


@pytest.fixture
def client():
    """The real app, with its lifespan, over a loopback peer.

    The lifespan is what opens the memory store, so using `TestClient` without a
    `with` block would test a deployment that cannot reach its own memory.
    """
    with TestClient(app, client=("127.0.0.1", 55555)) as test_client:
        yield test_client


class TestTheNodeIsInTheCatalogue:
    def test_it_appears_with_its_parameters(self, client):
        """What the palette reads. A node that runs but is not listed cannot be
        dragged onto a canvas, which makes it unreachable in practice."""
        payload = client.get("/api/node-types?lang=zh").json()
        types = payload["types"] if isinstance(payload, dict) and "types" in payload else payload
        by_type = {spec["type"]: spec for spec in types}

        assert "memory" in by_type, "the memory node is not offered by the catalogue"
        spec = by_type["memory"]
        assert spec["label"] != "Memory", "the Chinese label was not applied"
        assert spec["icon"] == "brain"
        names = {param["name"] for param in spec["params"]}
        assert {"mode", "namespace", "text", "query", "limit"} <= names


class TestAGraphThatUsesMemory:
    def test_a_written_fact_comes_back_out_of_the_end_node(self, client):
        """Proves the node is wired into the graph correctly: its output reaches
        the next node through the edge."""
        assert client.post("/api/workflows", json=_graph("wf-mem-1")).status_code == 201

        run = _run_to_completion(client, "wf-mem-1", FACT)
        assert run["status"] == "succeeded", run.get("error")
        assert run["outputs"] == {"memory": [FACT]}

    def test_a_second_run_recalls_what_the_first_run_wrote(self, client):
        """**The property that makes this memory.**

        Two separate runs. The second asks a *different question* and still gets
        the first run's fact back, because the match is by meaning. An
        implementation that stored facts in `vars` would pass every other test in
        this file and fail this one.
        """
        assert client.post("/api/workflows", json=_graph("wf-mem-2")).status_code == 201

        first = _run_to_completion(client, "wf-mem-2", FACT)
        assert first["status"] == "succeeded", first.get("error")

        second = _run_to_completion(client, "wf-mem-2", QUESTION)
        assert second["status"] == "succeeded", second.get("error")

        recalled = second["outputs"]["memory"]
        assert any("ACME" in item for item in recalled), (
            f"the second run did not recall the first run's fact: {recalled!r}"
        )

    def test_a_different_scope_does_not_see_the_fact(self, client):
        """Scopes keep one agent's facts out of another's prompt."""
        assert client.post("/api/workflows", json=_graph("wf-mem-a", "{agent}/a")).status_code == 201
        assert client.post("/api/workflows", json=_graph("wf-mem-b", "{agent}/b")).status_code == 201

        _run_to_completion(client, "wf-mem-a", FACT)
        other = _run_to_completion(client, "wf-mem-b", QUESTION)

        assert "ACME" not in json.dumps(other["outputs"], ensure_ascii=False)


class TestRunningWithoutMemory:
    def test_the_graph_still_runs_when_memory_is_off(self, client, monkeypatch):
        """One node's configuration must not become a graph-wide outage.

        This is the deployment that matters most for the claim: a customer who
        never configures memory still has to be able to run a workflow that
        contains a memory node.
        """
        from app.memory import NoMemoryStoreProvider, set_memory_store_provider

        set_memory_store_provider(NoMemoryStoreProvider())
        try:
            assert client.post("/api/workflows", json=_graph("wf-mem-off")).status_code == 201
            run = _run_to_completion(client, "wf-mem-off", FACT)

            assert run["status"] == "succeeded", run.get("error")
            # The end node received an empty list rather than the run failing.
            assert run["outputs"] == {"memory": []}
        finally:
            set_memory_store_provider(None)
