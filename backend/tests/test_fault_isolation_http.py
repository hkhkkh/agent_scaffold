"""Fault isolation through the real API: a node that really raises.

`test_fault_isolation.py` reads the source to pin the decision; this runs the
graph. Both are needed here, and the reason is measured rather than assumed: two
real defects during this change were only visible end-to-end.

1. The `node_finished` event overwrote the `node_failed` one, so a failed step
   showed as `succeeded` in the timeline and the only trace of the failure was a
   warning line. The source looked right.
2. The step recorder never read `outputs` off a `node_failed` payload, so the row
   said `failed` with null outputs — which reads as "produced nothing" rather
   than "produced the fallback".

Neither is a logic error in the policy, and neither would fail a unit test that
checks the policy object.

A Code node is the failure vehicle because it is the one node type whose behaviour
the user supplies, so it can be made to raise without a node type existing solely
for tests. Its `trusted` flag has to be set: a Code node runs author-supplied
Python and refuses to execute otherwise.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.runtime.attempts import ON_ERROR_CONTINUE, ON_ERROR_DEFAULT, ON_ERROR_STOP

#: Raises on purpose. The message is asserted on, so it has to be distinctive.
RAISES = "raise ValueError('this row is malformed')"


def _graph(
    workflow_id: str,
    *,
    on_error: str | None,
    on_error_value=None,
) -> dict:
    config: dict = {"code": RAISES, "language": "python"}
    if on_error is not None:
        config["on_error"] = on_error
    if on_error_value is not None:
        config["on_error_value"] = on_error_value
    return {
        "id": workflow_id,
        "name": "Fault probe",
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "label": "Start",
                "config": {"input_schema": {"query": ""}},
            },
            {
                "id": "boom",
                "type": "code",
                "label": "Failing step",
                "trusted": True,
                "config": config,
            },
            {
                "id": "end",
                "type": "end",
                "label": "End",
                "config": {"output_label": "result"},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "start",
                "sourceHandle": "out",
                "target": "boom",
                "targetHandle": "in",
            },
            {
                "id": "e2",
                "source": "boom",
                "sourceHandle": "out",
                "target": "end",
                "targetHandle": "in",
            },
        ],
    }


@pytest.fixture
def client(monkeypatch):
    """The real app, with the sandbox in-process so the probe stays fast.

    In-process is a development affordance and not a boundary — which is fine
    here, because these tests are about what happens *after* a node fails, not
    about containment.
    """
    monkeypatch.setenv("SANDBOX_MODE", "inprocess")
    from app.settings import get_settings

    get_settings.cache_clear()
    with TestClient(app, client=("127.0.0.1", 55555)) as test_client:
        yield test_client
    get_settings.cache_clear()


def _run(client: TestClient, workflow_id: str) -> dict:
    response = client.post(
        "/api/runs", json={"workflow_id": workflow_id, "inputs": {"query": "x"}}
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = client.get(f"/api/runs/{run_id}").json()
        if state["status"] in ("succeeded", "failed", "cancelled"):
            return state
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish")


class TestTheDefaultStillFailsTheRun:
    def test_a_node_with_no_on_error_fails_the_run(self, client):
        """The previous behaviour, and the reason this change is additive.

        A run that silently skips a step is how a workflow appears to work while
        returning wrong answers, so the default has to stay loud.
        """
        assert (
            client.post("/api/workflows", json=_graph("wf-stop", on_error=None)).status_code
            == 201
        )
        state = _run(client, "wf-stop")
        assert state["status"] == "failed"

    def test_an_explicit_stop_behaves_the_same(self, client):
        assert (
            client.post(
                "/api/workflows", json=_graph("wf-stop-explicit", on_error=ON_ERROR_STOP)
            ).status_code
            == 201
        )
        assert _run(client, "wf-stop-explicit")["status"] == "failed"

    def test_the_failure_names_the_node(self, client):
        """`[Failing step]` is what turns "something broke" into "this step
        broke", and it is the whole value of failing loudly."""
        assert (
            client.post("/api/workflows", json=_graph("wf-named", on_error=None)).status_code
            == 201
        )
        state = _run(client, "wf-named")
        assert "Failing step" in str(state["error"])

    def test_the_failure_reason_survives_into_the_message(self, client):
        """The node's own message must not be replaced by a generic one."""
        assert (
            client.post("/api/workflows", json=_graph("wf-why", on_error=None)).status_code
            == 201
        )
        assert "malformed" in str(_run(client, "wf-why")["error"])


class TestContinue:
    def test_the_run_succeeds(self, client):
        assert (
            client.post(
                "/api/workflows", json=_graph("wf-cont", on_error=ON_ERROR_CONTINUE)
            ).status_code
            == 201
        )
        state = _run(client, "wf-cont")
        assert state["status"] == "succeeded", state.get("error")

    def test_the_failed_step_is_recorded_as_failed(self, client):
        """Measured defect: the `node_finished` event used to overwrite the
        failure, so the timeline said the step succeeded."""
        assert (
            client.post(
                "/api/workflows", json=_graph("wf-step", on_error=ON_ERROR_CONTINUE)
            ).status_code
            == 201
        )
        state = _run(client, "wf-step")
        steps = client.get(f"/api/runs/{state['id']}/steps").json()["steps"]
        boom = next(step for step in steps if step["node_id"] == "boom")

        assert boom["status"] == "failed", (
            f"a failed step reported '{boom['status']}' — the timeline is lying"
        )

    def test_what_the_failed_step_produced_is_recorded(self, client):
        """Measured defect: the row said `failed` with null outputs, which reads
        as 'produced nothing' rather than 'produced the fallback'."""
        assert (
            client.post(
                "/api/workflows", json=_graph("wf-out", on_error=ON_ERROR_CONTINUE)
            ).status_code
            == 201
        )
        state = _run(client, "wf-out")
        steps = client.get(f"/api/runs/{state['id']}/steps").json()["steps"]
        boom = next(step for step in steps if step["node_id"] == "boom")

        assert boom["outputs"], "the failed step recorded no output at all"
        recorded = boom["outputs"].get("__error__")
        assert recorded, f"the failure was not recorded in the node output: {boom['outputs']}"
        assert "malformed" in json.dumps(recorded)

    def test_the_rest_of_the_graph_still_runs(self, client):
        """The point of the whole feature: a downstream step must still execute."""
        assert (
            client.post(
                "/api/workflows", json=_graph("wf-rest", on_error=ON_ERROR_CONTINUE)
            ).status_code
            == 201
        )
        state = _run(client, "wf-rest")
        steps = client.get(f"/api/runs/{state['id']}/steps").json()["steps"]
        by_id = {step["node_id"]: step for step in steps}

        assert by_id["end"]["status"] == "succeeded"
        # The run reached its end, which it would not have if the failure had
        # propagated.
        assert "result" in (state.get("outputs") or {})


class TestDefaultValue:
    def test_the_downstream_node_receives_the_nominated_value(self, client):
        assert (
            client.post(
                "/api/workflows",
                json=_graph(
                    "wf-def", on_error=ON_ERROR_DEFAULT, on_error_value="(unavailable)"
                ),
            ).status_code
            == 201
        )
        state = _run(client, "wf-def")
        assert state["status"] == "succeeded"
        assert state["outputs"]["result"] == "(unavailable)"

    def test_a_falsy_nominated_value_still_arrives(self, client):
        """`0` is a legitimate fallback — a failed count is zero, not nothing —
        and a truthiness check would silently turn it into `None`."""
        assert (
            client.post(
                "/api/workflows",
                json=_graph("wf-zero", on_error=ON_ERROR_DEFAULT, on_error_value=0),
            ).status_code
            == 201
        )
        state = _run(client, "wf-zero")
        assert state["outputs"]["result"] == 0


class TestControlFlowIsNotAffected:
    def test_a_run_still_reaches_its_end_without_a_failure(self, client):
        """The branch under test is in the same try/except as everything else, so
        a change here could break the happy path without failing a failure test.
        """
        graph = _graph("wf-happy", on_error=ON_ERROR_CONTINUE)
        graph["nodes"][1]["config"]["code"] = "out = {'ok': True}"
        assert client.post("/api/workflows", json=graph).status_code == 201

        state = _run(client, "wf-happy")
        assert state["status"] == "succeeded"
        steps = client.get(f"/api/runs/{state['id']}/steps").json()["steps"]
        boom = next(step for step in steps if step["node_id"] == "boom")
        assert boom["status"] == "succeeded"
        assert "__error__" not in json.dumps(boom["outputs"])
