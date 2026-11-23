"""Supervisor: routing to one worker, and *not* to the others.

The assertion that matters most here is a negative one — the unchosen worker must
**not** run. LangGraph's docs are explicit that `Command` adds a dynamic edge
rather than replacing static ones:

    `Command` only adds dynamic edges -- static edges defined with `add_edge`
    still execute. ... use either `Command` or static edges to route to the next
    nodes, not both.

So a supervisor whose workers are also wired to it would run them all. That looks
like harmless parallelism and is a routing bug, which is why it gets its own
test rather than being folded into "the chosen worker ran".

The negative assertion is also why these drive the real API instead of calling the
node: the node's return value is correct either way. It is the *compiler's* edge
construction that can be wrong, and only a run shows it.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.graph.compiler import DYNAMIC_ROUTING_TYPES, ROUTING_TYPES
from app.graph.schema import NodeSpec
from app.main import app
from app.nodes.supervisor import DECISION_KEY, DONE, MODE_EXPRESSION, MODE_LIST, SupervisorNode

# ---------------------------------------------------------------------------
# The node, in isolation
# ---------------------------------------------------------------------------


class _Ctx:
    """Stands in for `NodeContext`. Only the members this node reads."""

    def __init__(self, config: dict, inputs: dict | None = None, node_id="sup",
                 vars_: dict | None = None):
        self.node = NodeSpec(id=node_id, type="supervisor", label="Supervisor",
                             config=config)
        self.config = config
        self.inputs = inputs or {}
        self.state = {"workflow_id": "wf", "vars": vars_ or {}}
        from app.settings import get_settings

        self.settings = get_settings()
        self.emit = lambda *a, **k: None
        self.logs: list[tuple[str, str]] = []

        async def log(text: str, level: str = "info") -> None:
            self.logs.append((level, text))

        self.log = log
        self.node_outputs = None

    @property
    def vars(self) -> dict:
        return self.state.get("vars") or {}

    @property
    def messages(self) -> list:
        return []


def _node() -> SupervisorNode:
    return SupervisorNode(NodeSpec(id="sup", type="supervisor", label="S", config={}))


class TestDecidingInIsolation:
    async def test_a_matching_decision_is_returned(self):
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": "quick",
                    "workers": ["quick", "careful"]})
        result = await _node().run(ctx)
        assert result["out"] == "quick"
        assert result[DECISION_KEY] == "quick"

    async def test_done_is_returned_as_is(self):
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": DONE, "workers": ["a"]})
        assert (await _node().run(ctx))["out"] == DONE

    async def test_a_decision_naming_something_else_uses_the_fallback(self):
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": "typo",
                    "workers": ["quick", "careful"], "fallback": "careful"})
        result = await _node().run(ctx)
        assert result["out"] == "careful"
        assert any(level == "warn" for level, _ in ctx.logs)

    async def test_without_a_fallback_it_stops_instead_of_guessing(self):
        """Guessing a worker would silently do the wrong unit of work. Stopping
        is visible, and the log says why."""
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": "typo", "workers": ["quick"]})
        result = await _node().run(ctx)
        assert result["out"] == DONE
        assert any(level == "warn" for level, _ in ctx.logs)

    async def test_no_workers_at_all_is_an_error(self):
        """A supervisor with nothing to choose from cannot route, and that is the
        author's mistake — unlike an unroutable *decision*, which is data."""
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": "x", "workers": []})
        with pytest.raises(ValueError, match="lists no workers"):
            await _node().run(ctx)

    async def test_the_input_is_used_when_there_is_no_rule(self):
        """Lets a classifier node upstream drive the routing without a template."""
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": "", "workers": ["quick"]},
                   inputs={"in": "quick"})
        assert (await _node().run(ctx))["out"] == "quick"

    async def test_the_first_worker_is_the_default(self):
        """Order in `workers` is meaningful: it is how an author says 'usually
        go here'."""
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": "", "workers": ["first", "second"]})
        assert (await _node().run(ctx))["out"] == "first"

    async def test_a_decision_containing_a_worker_name_still_routes(self):
        """A rule that returns a sentence is common ('the quick path fits')."""
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": "use the careful path",
                    "workers": ["quick", "careful"]})
        assert (await _node().run(ctx))["out"] == "careful"

    async def test_list_mode_picks_the_declared_worker(self):
        ctx = _Ctx({"mode": MODE_LIST, "workers": ["only"]})
        assert (await _node().run(ctx))["out"] == "only"

    async def test_workers_may_be_given_as_a_comma_separated_string(self):
        """The canvas stores a JSON field, but a config written by hand or by the
        AI filler may be a string."""
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": "b", "workers": "a, b"})
        assert (await _node().run(ctx))["out"] == "b"


class TestTheHandoffLimit:
    async def test_a_spent_supervisor_stops_without_deciding(self):
        """A loop between two workers is the failure this bounds: it is the
        cheapest runaway to create and the most expensive to notice."""
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": "quick", "workers": ["quick"],
                    "max_handoffs": 3}, vars_={"sup": {"handoffs": 3}})
        result = await _node().run(ctx)
        assert result["out"] == DONE
        assert any(level == "warn" and "limit" in text for level, text in ctx.logs)

    async def test_the_count_is_carried_forward(self):
        """Read from `vars` because a node holds nothing between invocations."""
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": "quick", "workers": ["quick"],
                    "max_handoffs": 5}, vars_={"sup": {"handoffs": 2}})
        assert (await _node().run(ctx))["handoffs"] == 3

    async def test_a_junk_limit_does_not_disable_the_guard(self):
        """Falling back to a number keeps the guard on. Falling back to `0` —
        which means unlimited — would remove the protection because of a typo."""
        ctx = _Ctx({"mode": MODE_EXPRESSION, "decision": "a", "workers": ["a"],
                    "max_handoffs": "lots"}, vars_={"sup": {"handoffs": 0}})
        result = await _node().run(ctx)
        assert result["handoffs"] == 1


# ---------------------------------------------------------------------------
# The compiler's edge construction
# ---------------------------------------------------------------------------


class TestCompilerTreatsItAsDynamic:
    def test_the_type_is_declared_dynamic(self):
        assert "supervisor" in DYNAMIC_ROUTING_TYPES

    def test_it_is_not_lumped_with_the_branch_flag_types(self):
        """They are different mechanisms — one records a flag, the other returns
        the destination — and the compiler's job differs for each."""
        assert "supervisor" not in ROUTING_TYPES

    def test_a_supervisor_with_no_workers_still_gets_a_stop_edge(self):
        """Without it, a supervisor that decides the work is finished has nowhere
        to go and the graph cannot compile."""
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parents[1] / "app" / "graph" / "compiler.py"
        ).read_text(encoding="utf-8")
        start = source.index("def _add_supervisor_edges")
        body = source[start : source.index("__all__", start)]
        assert 'targets["done"] = END' in body


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def _graph(workflow_id: str, *, decision: str, workers: list[str],
           fallback: str = "") -> dict:
    """start -> supervisor -> (quick | careful) -> end.

    **Both workers are wired to the supervisor.** That is the shape that would
    double-run if the compiler drew the author's static edges, so the negative
    assertions below are testing the compiler, not the node.
    """
    return {
        "id": workflow_id,
        "name": "Supervisor probe",
        "nodes": [
            {"id": "start", "type": "start", "label": "Start",
             "config": {"input_schema": {"route": ""}}},
            {"id": "supervisor", "type": "supervisor", "label": "Dispatch",
             "config": {"mode": "expression", "decision": decision,
                        "workers": workers, "fallback": fallback,
                        "max_handoffs": 5}},
            {"id": "quick", "type": "prompt", "label": "Quick",
             "config": {"template": "QUICK"}},
            {"id": "careful", "type": "prompt", "label": "Careful",
             "config": {"template": "CAREFUL"}},
            {"id": "end", "type": "end", "label": "End",
             "config": {"output_label": "result"}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "out",
             "target": "supervisor", "targetHandle": "in"},
            {"id": "e2", "source": "supervisor", "sourceHandle": "out",
             "target": "quick", "targetHandle": "in"},
            {"id": "e3", "source": "supervisor", "sourceHandle": "out",
             "target": "careful", "targetHandle": "in"},
            {"id": "e4", "source": "quick", "sourceHandle": "out",
             "target": "end", "targetHandle": "in"},
            {"id": "e5", "source": "careful", "sourceHandle": "out",
             "target": "end", "targetHandle": "in"},
        ],
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "inprocess")
    from app.settings import get_settings

    get_settings.cache_clear()
    with TestClient(app, client=("127.0.0.1", 55555)) as test_client:
        yield test_client
    get_settings.cache_clear()


def _run(client: TestClient, workflow_id: str) -> tuple[dict, list[str]]:
    response = client.post(
        "/api/runs", json={"workflow_id": workflow_id, "inputs": {"route": "x"}}
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = client.get(f"/api/runs/{run_id}").json()
        if state["status"] in ("succeeded", "failed", "cancelled"):
            steps = client.get(f"/api/runs/{run_id}/steps").json()["steps"]
            return state, [step["node_id"] for step in steps]
        time.sleep(0.2)
    raise AssertionError("run did not finish")


class TestRoutingEndToEnd:
    def test_the_chosen_worker_runs(self, client):
        assert client.post("/api/workflows", json=_graph(
            "wf-pick-a", decision="quick", workers=["quick", "careful"])).status_code == 201
        state, ran = _run(client, "wf-pick-a")
        assert state["status"] == "succeeded", state.get("error")
        assert "quick" in ran

    def test_the_unchosen_worker_does_not_run(self, client):
        """★ The whole reason `DYNAMIC_ROUTING_TYPES` exists.

        Both workers are wired to the supervisor. If the compiler drew those
        edges as well as installing the conditional one, `careful` would run too
        and the graph would appear to work while doing extra work.
        """
        assert client.post("/api/workflows", json=_graph(
            "wf-pick-b", decision="quick", workers=["quick", "careful"])).status_code == 201
        _, ran = _run(client, "wf-pick-b")
        assert "careful" not in ran, f"the unchosen worker ran: {ran}"

    def test_picking_the_other_worker_also_excludes_the_first(self, client):
        """The mirror case, so a fix that hard-codes one direction fails."""
        assert client.post("/api/workflows", json=_graph(
            "wf-pick-c", decision="careful", workers=["quick", "careful"])).status_code == 201
        _, ran = _run(client, "wf-pick-c")
        assert "careful" in ran
        assert "quick" not in ran, f"the unchosen worker ran: {ran}"

    def test_the_fallback_runs_when_the_decision_is_unknown(self, client):
        assert client.post("/api/workflows", json=_graph(
            "wf-fb", decision="nonsense", workers=["quick", "careful"],
            fallback="careful")).status_code == 201
        _, ran = _run(client, "wf-fb")
        assert "careful" in ran
        assert "quick" not in ran

    def test_an_unknown_decision_without_a_fallback_still_finishes(self, client):
        """A supervisor boxed in by its own declaration should end the run, not
        fail it — the log says what happened."""
        assert client.post("/api/workflows", json=_graph(
            "wf-stop", decision="nonsense", workers=["quick", "careful"])).status_code == 201
        state, ran = _run(client, "wf-stop")
        assert state["status"] == "succeeded", state.get("error")
        assert "quick" not in ran and "careful" not in ran

    def test_done_runs_no_worker(self, client):
        assert client.post("/api/workflows", json=_graph(
            "wf-done", decision=DONE, workers=["quick", "careful"])).status_code == 201
        state, ran = _run(client, "wf-done")
        assert state["status"] == "succeeded"
        assert "quick" not in ran and "careful" not in ran

    def test_workers_declared_only_in_config_still_route(self, client):
        """A graph that lists workers in the node and draws no edges is a
        legitimate way to build it, and the compiler takes the union."""
        graph = _graph("wf-config-only", decision="careful", workers=["quick", "careful"])
        graph["edges"] = [
            edge for edge in graph["edges"] if edge["source"] != "supervisor"
        ]
        assert client.post("/api/workflows", json=graph).status_code == 201
        state, ran = _run(client, "wf-config-only")
        assert state["status"] == "succeeded", state.get("error")
        assert "careful" in ran
