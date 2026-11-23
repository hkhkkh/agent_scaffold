"""Stopping a run before it overruns, and the two defects found doing it.

The feature is small — a forecast plus a policy — but building it uncovered two
real defects in code it depended on, and both are pinned here because neither was
found by the tests that existed:

1. **A node's log lines were never recorded.** `StepRecord` had no `logs` field
   and the event recorder had no `log` branch, so `ctx.log(...)` reached the live
   event stream and was then dropped. Every step of every run reported
   `logs: None` — 16 node types' worth of diagnostics, invisible after reload.

2. **A `log` arriving before `node_started` was discarded.** The forecast has to
   run before the node it is forecasting for, so its warning arrives with no step
   record yet — and the recorder returned early. The first fix (keeping the line)
   exposed a second: `node_started` replaced the record, dropping it again.

Both were invisible to the existing suite because it asserted on outputs, statuses
and errors, never on what a node said.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.graph.compiler import _report_projection, wind_down_stops
from app.main import app
from app.runtime.budget import Budget, BudgetExceeded
from app.runtime.projection import (
    DEFAULT_HEADROOM,
    FORECAST_STEPS,
    WIND_DOWN_STOP,
    WIND_DOWN_WARN,
    Projection,
    WindDown,
    project,
    wind_down_mode,
)
from app.settings import Settings

# ---------------------------------------------------------------------------
# The forecast
# ---------------------------------------------------------------------------


class TestProjection:
    def test_a_budget_with_no_limits_projects_nothing(self):
        """Free to call on every node, which is what makes it usable here."""
        projection = project(Budget())
        assert projection.should_wind_down is False
        assert projection.tightest is None
        assert projection.used_fraction == {}

    def test_none_is_accepted(self):
        """A graph compiled outside a run has no budget at all."""
        assert project(None).should_wind_down is False

    def test_a_roomy_budget_is_left_alone(self):
        """The failure a naive forecast has is warning constantly, which trains
        the reader to ignore it."""
        budget = Budget(max_steps=100)
        budget.steps = 3
        assert project(budget).should_wind_down is False

    def test_a_budget_about_to_run_out_is_flagged(self):
        budget = Budget(max_steps=8)
        budget.steps = 5
        projection = project(budget)
        assert projection.should_wind_down is True
        assert projection.tightest == "steps"
        assert "run out at step 8" in " ".join(projection.reasons)

    def test_the_boundary_is_inclusive(self):
        """Measured defect: the first version compared a projected *percentage*
        against 1.0, so a run with exactly 3 steps of budget left and 3 steps
        planned came out at 1.0 and `>` missed it. Expressed as a count, the
        comparison is `<=` and the boundary is right."""
        budget = Budget(max_steps=10)
        budget.steps = 7  # 30% left, 10% per step → exactly 3 steps
        assert project(budget).should_wind_down is True

    def test_the_headroom_alone_can_trigger_it(self):
        """Even when the per-step rate is unknown (one step so far), a thin
        remainder is worth saying out loud."""
        budget = Budget(max_steps=10)
        budget.steps = 9
        assert project(budget).should_wind_down is True

    def test_an_unset_limit_is_not_inspected(self):
        budget = Budget(max_steps=8)
        budget.steps = 5
        assert "tokens" not in project(budget).used_fraction

    def test_unreported_tokens_do_not_count_as_zero(self):
        """A provider that never reported usage must not look like a run that
        used none — the same rule `budget.check` follows."""
        budget = Budget(max_tokens=1_000_000)
        budget.add_usage(None)  # marks tokens_known False
        projection = project(budget)
        assert "tokens" not in projection.used_fraction

    def test_wall_clock_is_not_extrapolated_by_step_count(self):
        """Wall-clock grows with *time*, not with steps, so extrapolating it per
        step would be wrong in exactly the case that matters — one slow node."""
        budget = Budget(max_wall_seconds=1000)
        budget.steps = 5
        reasons = " ".join(project(budget).reasons)
        assert "wall-clock" not in reasons or "run out at step" not in reasons

    def test_the_projection_serialises_for_a_client(self):
        budget = Budget(max_steps=8)
        budget.steps = 6
        payload = project(budget).as_dict()
        assert set(payload) == {
            "used_fraction",
            "tightest",
            "remaining_fraction",
            "should_wind_down",
            "reasons",
        }
        import json

        json.dumps(payload)  # must survive the wire

    def test_it_is_a_plain_dataclass_of_facts(self):
        """No behaviour on it, so a caller cannot accidentally treat the *result*
        as the policy."""
        budget = Budget(max_steps=8)
        budget.steps = 6
        projection = project(budget)
        assert isinstance(projection, Projection)
        assert not hasattr(projection, "act")


class TestTheMode:
    def test_the_default_is_to_warn_rather_than_stop(self):
        """A forecast is an extrapolation. Ending a run on one would be a guess,
        and the reactive ceiling still applies underneath."""
        assert wind_down_mode(Settings()) == WIND_DOWN_WARN

    def test_stop_is_honoured(self):
        assert wind_down_mode(Settings(budget_wind_down="stop")) == WIND_DOWN_STOP

    def test_an_unknown_mode_becomes_warn(self):
        """`stop` ends a run; doing that because of a typo in an optional field
        would lose work the ceiling would have allowed."""
        assert wind_down_mode(Settings(budget_wind_down="stpo")) == WIND_DOWN_WARN


class TestTheStopDecision:
    def test_warn_never_stops(self):
        budget = Budget(max_steps=8)
        budget.steps = 6
        assert wind_down_stops(budget, Settings(budget_wind_down="warn")) is False

    def test_stop_stops_when_the_forecast_says_so(self):
        budget = Budget(max_steps=8)
        budget.steps = 6
        assert wind_down_stops(budget, Settings(budget_wind_down="stop")) is True

    def test_stop_does_not_stop_a_roomy_run(self):
        budget = Budget(max_steps=100)
        budget.steps = 3
        assert wind_down_stops(budget, Settings(budget_wind_down="stop")) is False


class TestTheException:
    def test_it_is_not_a_budget_exceeded(self):
        """The distinction the whole feature rests on: an exceeded budget is a
        run that went too far, a wind-down is one that was steered. Reporting the
        second as the first tells a caller their workflow broke."""
        assert not issubclass(WindDown, BudgetExceeded)

    def test_it_carries_the_forecast(self):
        wind_down = WindDown(("about to run out",))
        assert wind_down.reasons == ("about to run out",)
        assert "run out" in str(wind_down)


@pytest.mark.asyncio
class TestTheEvent:
    async def test_a_roomy_budget_emits_nothing(self):
        emitted: list = []

        async def emit(kind, data):
            emitted.append((kind, data))

        budget = Budget(max_steps=100)
        budget.steps = 2
        await _report_projection(budget, Settings(), emit)
        assert emitted == []

    async def test_a_tight_budget_emits_a_warning_with_the_forecast(self):
        emitted: list = []

        async def emit(kind, data):
            emitted.append((kind, data))

        budget = Budget(max_steps=8)
        budget.steps = 6
        await _report_projection(budget, Settings(), emit)

        assert len(emitted) == 1
        kind, data = emitted[0]
        assert kind == "log"
        assert data["level"] == "warning"
        assert data["projection"]["should_wind_down"] is True


# ---------------------------------------------------------------------------
# The two defects, end to end
# ---------------------------------------------------------------------------


CHAIN = 6


def _graph(workflow_id: str) -> dict:
    nodes = [{"id": "start", "type": "start", "label": "Start",
              "config": {"input_schema": {"query": ""}}}]
    for index in range(CHAIN):
        nodes.append({"id": f"s{index}", "type": "prompt", "label": f"Step {index}",
                      "config": {"template": f"step-{index}"}})
    nodes.append({"id": "end", "type": "end", "label": "End",
                  "config": {"output_label": "result"}})
    edges = [{"id": "e0", "source": "start", "sourceHandle": "out",
              "target": "s0", "targetHandle": "in"}]
    for index in range(CHAIN):
        nxt = f"s{index + 1}" if index + 1 < CHAIN else "end"
        edges.append({"id": f"e{index + 1}", "source": f"s{index}",
                      "sourceHandle": "out", "target": nxt, "targetHandle": "in"})
    return {"id": workflow_id, "name": "Budget probe", "nodes": nodes, "edges": edges}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "inprocess")
    monkeypatch.setenv("MEMORY_STORE", "memory")
    from app.settings import get_settings

    get_settings.cache_clear()
    with TestClient(app, client=("127.0.0.1", 55555)) as test_client:
        yield test_client
    get_settings.cache_clear()


def _run(client: TestClient, workflow_id: str) -> tuple[dict, list[dict]]:
    response = client.post(
        "/api/runs", json={"workflow_id": workflow_id, "inputs": {"query": "x"}}
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = client.get(f"/api/runs/{run_id}").json()
        if state["status"] in ("succeeded", "failed", "stopped", "cancelled"):
            return state, client.get(f"/api/runs/{run_id}/steps").json()["steps"]
        time.sleep(0.2)
    raise AssertionError("run did not finish")


class TestLogsAreRecorded:
    def test_a_node_s_own_log_line_is_in_the_step_list(self, client):
        """★ Defect 1. Every node's diagnostics were being dropped, so a step
        gave its inputs and outputs and nothing about what it did with them."""
        assert client.post("/api/workflows", json=_graph("wf-logs")).status_code == 201
        _, steps = _run(client, "wf-logs")

        with_logs = [step for step in steps if step.get("logs")]
        assert with_logs, (
            "no step carried a log line — the recorder is dropping `log` events "
            f"again: {[step.get('logs') for step in steps]}"
        )
        # A prompt node logs what it rendered, so at least one line is expected.
        assert any("Rendered" in line for step in steps for line in step.get("logs") or [])

    def test_logs_survive_a_reload_from_the_store(self, client):
        """The live shape and the stored shape have to agree, or a client sees a
        step's logs while it runs and loses them on refresh."""
        assert client.post("/api/workflows", json=_graph("wf-rel")).status_code == 201
        state, live = _run(client, "wf-rel")

        # Evict the in-memory run, then read it back from the store.
        from app.runtime.manager import get_run_manager

        manager = get_run_manager()
        context = manager.get(state["id"])
        if context is not None:
            for run_id in list(getattr(manager, "_runs", {})):
                if run_id == state["id"]:
                    manager._runs.pop(run_id, None)

        reloaded = client.get(f"/api/runs/{state['id']}/steps").json()["steps"]
        live_lines = [line for step in live for line in step.get("logs") or []]
        stored_lines = [line for step in reloaded for line in step.get("logs") or []]
        assert live_lines, "the live run recorded no logs, so this proves nothing"
        assert stored_lines == live_lines, (
            "the stored steps lost their logs — the two shapes disagree"
        )


class TestTheWarningIsVisible:
    def test_a_tight_budget_produces_a_warning_in_the_step_list(self, client, monkeypatch):
        """★ Defect 2, and the reason the feature needed the first fix to be
        observable at all. The forecast emits *before* the node it is forecasting
        for, so its line arrives with no step record yet."""
        monkeypatch.setenv("BUDGET_WIND_DOWN", "warn")
        monkeypatch.setenv("MAX_RUN_STEPS", str(CHAIN + 2))
        from app.settings import get_settings

        get_settings.cache_clear()

        assert client.post("/api/workflows", json=_graph("wf-warn")).status_code == 201
        state, steps = _run(client, "wf-warn")

        lines = [line for step in steps for line in step.get("logs") or []]
        warnings = [line for line in lines if "Budget nearly spent" in line]
        assert warnings, (
            "the forecast fired (it fires from step 5 of 8) but no warning "
            f"reached the timeline. Lines seen: {lines}"
        )
        # Prefixed, so a reader can tell it from an informational line in a plain
        # list of strings.
        assert any(line.startswith("[warning]") for line in warnings)


class TestStopping:
    def test_stop_ends_the_run_early_with_its_status_apart_from_failure(self, client, monkeypatch):
        monkeypatch.setenv("BUDGET_WIND_DOWN", "stop")
        monkeypatch.setenv("MAX_RUN_STEPS", "3")
        from app.settings import get_settings

        get_settings.cache_clear()

        assert client.post("/api/workflows", json=_graph("wf-stp")).status_code == 201
        state, steps = _run(client, "wf-stp")

        assert state["status"] == "stopped", (
            f"expected `stopped` (neither succeeded nor failed), got "
            f"{state['status']!r}: {state.get('error')}"
        )
        assert len(steps) < CHAIN + 2, f"the run did not stop early: {len(steps)} steps"
        assert "budget" in str(state.get("error", "")).lower()

    def test_a_stopped_run_keeps_its_output(self, client, monkeypatch):
        """'Finish with what you have' has to mean it, or the forecast is only a
        tidier way to lose the same work."""
        monkeypatch.setenv("BUDGET_WIND_DOWN", "stop")
        monkeypatch.setenv("MAX_RUN_STEPS", "3")
        from app.settings import get_settings

        get_settings.cache_clear()

        assert client.post("/api/workflows", json=_graph("wf-keep")).status_code == 201
        state, _ = _run(client, "wf-keep")
        assert state.get("outputs"), "a stopped run kept nothing"

    def test_an_unlimited_run_is_untouched(self, client):
        """The feature is opt-in by virtue of needing a limit, so a deployment
        that configures none sees exactly the old behaviour."""
        assert client.post("/api/workflows", json=_graph("wf-none")).status_code == 201
        state, steps = _run(client, "wf-none")
        assert state["status"] == "succeeded"
        assert len(steps) == CHAIN + 2
