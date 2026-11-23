"""Trial-run a generated workflow before handing it to the user.

Validation answers "would this graph compile". It cannot answer "would this
agent do its job": an unset input default, a prompt that renders empty, a Code
node reading the wrong shape — all of those compile, and all of them fail at the
first real run. The generator therefore runs the draft once, on its own declared
defaults, and feeds a failure back to the model like any other correction.

Two properties this deliberately does **not** have:

* **It is not free.** The trial executes nodes, which means model calls, tool
  calls and whatever side effects the graph has (a database write, for one).
  That is the point — a test that stubs the effects cannot prove the agent works
  — but it is why the caller can turn it off and why the time budget is bounded.
* **It does not trust.** Code nodes remain untrusted, so a graph containing one
  fails validation and never reaches this module. Auto-trusting so the test could
  run would defeat the only human gate the platform has.

The result is shaped for two readers at once: the dialog renders the short form,
and :func:`trial_feedback` turns the same dict into instructions for the model.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from app.graph.compiler import CompileError, compile_graph
from app.graph.schema import WorkflowGraph
from app.runtime.budget import Budget, clear_budget, register_budget
from app.settings import get_settings

#: Cap on retained events. A long graph produces one event per node plus every
#: log line; nobody reads the middle, and the correction message only carries a
#: tail. Small enough that the trial result stays cheap to put in an API response.
_MAX_EVENTS = 300
_TAIL_EVENTS = 12
_TAIL_LOGS = 12

#: Failures that come from outside the graph. Correcting the graph cannot fix a
#: dead network or a rejected API key, and asking the model to "fix it" invites
#: exactly the wrong edit — deleting the node that called the service. Matched
#: case-insensitively against the error text.
_ENVIRONMENT_MARKERS = (
    "timeout",
    "timed out",
    "connecterror",
    "connection",
    "getaddrinfo",
    "ssl",
    "network",
    "rate limit",
    "429",
    "unauthorized",
    "401",
    "403",
    "authentication",
    "api key",
    "dns",
)


def looks_environmental(error: str) -> bool:
    """Whether a failure is the deployment's fault rather than the graph's."""
    lowered = (error or "").lower()
    return any(marker in lowered for marker in _ENVIRONMENT_MARKERS)


async def trial_run(
    graph: WorkflowGraph,
    *,
    timeout_seconds: float | None = None,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute ``graph`` once and report what happened.

    ``inputs`` defaults to ``{}``: the Start node's own declared defaults are
    what a user gets on the first press of Run, so they are what the test should
    exercise. Passing values here would test a workflow nobody is about to run.
    """
    settings = get_settings()
    timeout = float(
        timeout_seconds
        if timeout_seconds is not None
        else settings.ai_self_test_timeout_seconds
    )

    events: list[dict[str, Any]] = []
    #: Monotonic start per node, so each finished event can carry a duration.
    #: A loop re-runs the same id, so the latest start wins; that is also what
    #: the UI pairs the finish with.
    node_started_at: dict[str, float] = {}

    async def emit(
        kind: str, data: dict[str, Any], node_id: str, node_label: str
    ) -> None:
        if kind not in {"node_started", "node_finished", "node_failed", "log"}:
            return
        at_ms = round((time.perf_counter() - started) * 1000, 1)
        entry: dict[str, Any] = {
            "kind": kind,
            "node_id": node_id,
            "node": node_label,
            "at_ms": at_ms,
        }
        if kind == "node_started":
            node_started_at[node_id] = time.perf_counter()
            entry["inputs"] = _preview(data.get("inputs"))
        elif kind == "node_finished":
            entry["outputs"] = _preview(data.get("outputs"))
            began = node_started_at.pop(node_id, None)
            if began is not None:
                entry["duration_ms"] = round((time.perf_counter() - began) * 1000, 1)
        elif kind == "node_failed":
            entry["error"] = str(data.get("error") or "")
            if data.get("outputs") is not None:
                entry["outputs"] = _preview(data.get("outputs"))
            began = node_started_at.pop(node_id, None)
            if began is not None:
                entry["duration_ms"] = round((time.perf_counter() - began) * 1000, 1)
        else:
            entry["message"] = str(data.get("message") or "")
            entry["level"] = str(data.get("level") or "info")
        events.append(entry)
        if len(events) > _MAX_EVENTS:
            del events[: len(events) - _MAX_EVENTS]

    started = time.perf_counter()
    status = "passed"
    error: str | None = None
    failed_node: str | None = None
    reason: str | None = None
    outputs: dict[str, Any] = {}
    interrupted = False

    try:
        compiled = compile_graph(graph, emitter=emit)
    except CompileError as exc:
        return {
            "status": "skipped",
            "ok": False,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": str(exc),
            "failed_node": None,
            "reason": "compile_error",
            "environment": False,
            "outputs": {},
            "events": [],
            "summary": f"Could not compile for a trial run: {exc}",
        }

    run_id = f"selftest-{uuid.uuid4().hex[:12]}"
    budget = Budget.from_settings(settings)
    # A trial is bounded even when the deployment configures no run limit: the
    # person waiting in the generate dialog is the budget.
    budget.max_wall_seconds = (
        timeout
        if budget.max_wall_seconds is None
        else min(budget.max_wall_seconds, timeout)
    )
    register_budget(run_id, budget)

    try:
        result = await asyncio.wait_for(
            compiled.ainvoke(
                {"inputs": dict(inputs or {})},
                config={
                    "configurable": {"thread_id": run_id, "run_id": run_id},
                    "recursion_limit": settings.recursion_limit,
                },
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        status = "failed"
        error = f"Trial run exceeded its {timeout:g}s time limit."
        reason = "timeout"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the failure is the result
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
    else:
        state, interrupts = _split_result(result)
        interrupted = bool(interrupts)
        raw_output = state.get("output") if isinstance(state, dict) else None
        outputs = raw_output if isinstance(raw_output, dict) else (
            {"out": raw_output} if raw_output is not None else {}
        )
        if interrupted:
            # Waiting for a human is the designed behaviour of a Human Input
            # node, not a defect in the graph.
            status = "paused"
            reason = "awaiting_human_input"
    finally:
        clear_budget(run_id)

    # A node can be configured to continue after failure. The run then reports
    # success while a step silently produced its failure value; for a generated
    # graph that is a defect worth surfacing, so the failed event wins.
    failures = [event for event in events if event["kind"] == "node_failed"]
    if failures:
        first = failures[0]
        failed_node = first.get("node_id")
        if status == "passed":
            status = "failed"
        error = error or first.get("error") or "A node failed."
        reason = reason or "node_failed"

    environment = looks_environmental(error or "")
    duration_ms = round((time.perf_counter() - started) * 1000, 1)

    return {
        "status": status,
        "ok": status == "passed",
        "duration_ms": duration_ms,
        "error": error,
        "failed_node": failed_node,
        "failed_node_label": next(
            (e["node"] for e in events if e["node_id"] == failed_node), None
        ),
        "reason": reason,
        "environment": environment,
        "interrupted": interrupted,
        "outputs": _preview(outputs),
        # `events` is the tail the model needs to correct a failure. `trace` is
        # the full bounded timeline the UI shows in the Run tab — same source,
        # two readers with different size budgets.
        "trace": list(events),
        "events": _tail(events),
        "summary": _summary(status, error, failed_node, environment, duration_ms),
    }


# ---------------------------------------------------------------------------
# Model feedback
# ---------------------------------------------------------------------------


def trial_feedback(result: dict[str, Any]) -> str:
    """Turn a failed trial into instructions for the generating model."""
    lines = [
        "The graph passes validation but its trial run failed, so the agent is "
        "not usable yet.",
        "",
    ]
    if result.get("failed_node_label"):
        lines.append(f"Failing node: {result['failed_node_label']} "
                     f"({result.get('failed_node')})")
    lines.append(f"Error: {result.get('error')}")
    lines.append("")

    logs = [e for e in result.get("events") or [] if e["kind"] == "log"]
    if logs:
        lines.append("Last log lines:")
        for entry in logs[-_TAIL_LOGS:]:
            lines.append(f"- [{entry['node']}] {entry['message']}")
        lines.append("")

    failed = next(
        (e for e in result.get("events") or [] if e["kind"] == "node_failed"), None
    )
    if failed and failed.get("outputs"):
        lines.append(f"The node produced this instead of its value: "
                     f"{failed['outputs']}")
        lines.append("")

    lines += [
        "Fix the graph with the tools, then call `finish` again. The trial run "
        "will be repeated.",
        "Rules:",
        "- Fix the cause, not the symptom. A template reading the wrong field is "
        "a path bug, not a reason to delete the node.",
        "- Use `inspect_canvas` to re-read the wiring and, for a Code node, "
        "remember it must be trusted by a human before the run can execute it.",
        "- Give the Start node realistic non-empty defaults: the trial runs on "
        "them, and so will the first user.",
        "- If the error is external (network, timeout, authentication, rate "
        "limit) the graph probably is not at fault. Do not change the graph; "
        "call `finish` and say so in the summary.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_result(result: Any) -> tuple[dict[str, Any], list[Any]]:
    """Same normalisation as the run manager, kept local to avoid importing it.

    Importing `app.runtime.manager` from here would drag the run store and the
    queue into every generation call for one eight-line helper.
    """
    if hasattr(result, "value") and hasattr(result, "interrupts"):
        state = result.value if isinstance(result.value, dict) else {}
        return state, list(result.interrupts or ())
    if isinstance(result, dict):
        return result, list(result.get("__interrupt__") or ())
    return {}, []


def _preview(value: Any, limit: int = 600) -> Any:
    """Keep a value recognisable, JSON-safe, and cheap enough for a transcript."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)[:limit]
    if len(text) <= limit:
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text[:limit].rstrip() + "…"


def _tail(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return events[-_TAIL_EVENTS:]


def _summary(
    status: str,
    error: str | None,
    failed_node: str | None,
    environment: bool,
    duration_ms: float,
) -> str:
    if status == "passed":
        return f"Trial run passed in {duration_ms / 1000:.1f}s."
    if status == "paused":
        return "Trial run paused at a Human Input node, as designed."
    where = f" at '{failed_node}'" if failed_node else ""
    if environment:
        return (
            f"Trial run failed{where} after {duration_ms / 1000:.1f}s. The error "
            f"looks external, so the graph was left unchanged."
        )
    return f"Trial run failed{where} after {duration_ms / 1000:.1f}s: {error}"


__all__ = ["looks_environmental", "trial_feedback", "trial_run"]
