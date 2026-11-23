"""What the build-time AI is told about the platform it is editing for.

The canvas AI used to see only the node catalogue and the current graph. That is
enough to draw a valid graph and not enough to make a deliverable one: it did not
know that continuous operation is a schedule rather than a loop, that one run is
bounded, that there is no sleep node, or that exported packages have their own
daemon. It would happily build a graph that looked continuous and could not be.

This module generates that missing context from the **live** deployment instead
of a paragraph in the prompt that drifts:

* limits come from settings and the node spec, so raising one changes the brief;
* schedule vocabulary comes from the scheduling rules;
* Code/scheduler/public-demo posture comes from the same flags the API enforces;
* the export section describes the package the exporter actually writes.

It is deliberately read-only and cheap. `platform_tools` is the separate module
that lets the copilot act on this knowledge.
"""

from __future__ import annotations

from typing import Any

from app.nodes import get_node_type_spec, has_node_type
from app.scheduling.rules import FREQUENCIES
from app.settings import get_settings


def _loop_ceiling() -> int:
    """The Loop node's hard maximum, read from the spec rather than repeated."""
    if not has_node_type("loop"):
        return 0
    for param in get_node_type_spec("loop").params:
        if getattr(param, "name", "") == "max_iterations":
            return int(getattr(param, "max", 0) or 0)
    return 0


def _budget(name: str, value: float, unit: str) -> str:
    if not value or value <= 0:
        return f"{name}: unlimited in this deployment"
    return f"{name}: {value:g} {unit}"


def platform_brief() -> str:
    """A compact, live description of how this platform runs a workflow."""
    settings = get_settings()
    loop_max = _loop_ceiling()
    code = "enabled" if settings.code_node_enabled else "DISABLED"
    scheduling = (
        "enabled" if settings.scheduler_enabled else "DISABLED"
    )
    public = "on (safe demo; Code and scheduling are restricted)" if settings.public_demo else "off"
    frequencies = ", ".join(FREQUENCIES)

    return f"""\
## Platform facts (live, generated from this deployment)

The canvas edits a definition. The backend executes it. They are not the same
lifetime, and confusing them is the most expensive mistake you can make here.

1. **One run is bounded and finite.** It starts at Start, ends at End, and is
   subject to: {_budget("recursion_limit", settings.recursion_limit, "super-steps")};
   {_budget("MAX_RUN_SECONDS", settings.max_run_seconds, "s")};
   {_budget("MAX_RUN_STEPS", settings.max_run_steps, "node executions")};
   {_budget("MAX_RUN_TOKENS", settings.max_run_tokens, "tokens")}.
2. **Repetition is a trigger on the workflow, not an edge in the graph.** The
   scheduler ({scheduling}) can fire a saved workflow {frequencies} (minimum
   interval 1 minute). The graph runs once per fire. A schedule is a platform
   object, not a node and not an edge.
3. **Loops are only for bounded iteration inside one run.** The Loop node has a
   hard ceiling of {loop_max or "n/a"} passes, and a cycle without a Loop node is
   rejected. There is **no Wait/Delay node**: a loop spins as fast as its nodes
   execute. Do not use a loop as an outer "run forever / every minute" mechanism.
4. **Exported packages have their own daemon.** `main.py daemon --interval ...
   --daily-at ...` runs one bounded cycle per interval, writes
   `data/agent_cycles.db` + `health.json`, and keeps a once-per-day marker. That
   is a deployment action, outside the graph; you cannot create it from a node.
5. **Business data goes to `results.db`** through `db_write`/`db_query` tools;
   workflow versions and run history are platform objects. Do not invent a
   database node.
6. **Saving is a separate human step.** An AI edit is a draft until the user
   saves it; a schedule created during an edit points at the last saved version
   until then. Version history exists, but the AI does not roll it back.
7. **A Human node waits for a person, not a timer.** It interrupts the run and
   resumes with an answer; it is not a way to pause for an hour.
8. **Code nodes are {code}** in this deployment; scheduling is {scheduling};
   public demo is {public}.
9. **No OPC UA / MQTT / Modbus / PLC connector exists.** Do not fake a real
   production-system write-back with `http_request` or Code. Name the missing
   connector in `open_questions` and build the supported part.

### Decision rules

| The user asks for | Do this |
|---|---|
| "monitor continuously", "24/7", "every N minutes", "run for a year" | Build **one control cycle** as a normal DAG. Then tell them to put a schedule on it (platform) or use the export daemon. Do not add a Loop as the outer mechanism. |
| "retry up to N times", "refine until the score passes", "poll at most N times" | Use the Loop node with an explicit `max_iterations` and a condition. Keep the body deterministic where possible. |
| "write to a production system / read a PLC" | Explain that the connector is missing. Do not simulate it silently. |
| "make a one-off report / analyze this data" | A plain pipeline ending in End; no loop and no schedule. |

When you propose a schedule, state the frequency, the time zone, the exact
inputs, and that the workflow must be **saved before the schedule fires**. A
schedule you describe but did not create is a proposal, not a fact.
"""


def workflow_context(workflow_id: str) -> str:
    """The brief plus this workflow's triggers and current status.

    Read-only. The copilot can call this to answer "is it scheduled?" without
    guessing, and a missing answer is better than an invented one.
    """
    from app.scheduling import store

    try:
        rows = [
            row
            for row in store.list_schedules()
            if row.get("workflow_id") == workflow_id
        ]
    except Exception as exc:  # noqa: BLE001 - the brief is still useful
        rows = []
        triggers = f"Could not read the schedule store ({exc}). Do not guess."
    else:
        triggers = ""
    if not triggers and not rows:
        triggers = "No triggers are registered for this workflow."
    elif rows:
        lines = ["id | enabled | status | rule | next fire | last run | last error"]
        for row in rows:
            rule = row.get("rule") or {}
            lines.append(
                f"{row.get('id')} | {row.get('enabled')} | {row.get('status')} | "
                f"{rule} | {row.get('next_run_at') or '-'} | "
                f"{row.get('last_run_id') or '-'} | {row.get('last_error') or '-'}"
            )
        triggers = "\n".join(lines)

    return platform_brief() + f"""

## This workflow's triggers

workflow_id: {workflow_id}
{triggers}

A trigger's inputs must match the workflow's Start `input_schema`; otherwise the
first fire fails with a validation error rather than doing the work.
"""


def deployment_plan(
    workflow_id: str,
    *,
    interval: int = 60,
    daily_at: str = "",
    timezone: str = "",
    inputs: dict[str, Any] | None = None,
    daily_inputs: dict[str, Any] | None = None,
) -> str:
    """The exact daemon command for an exported package — a proposal, not a run."""
    import json

    if interval < 1:
        raise ValueError("interval must be at least 1 second")
    if daily_at and daily_inputs is None:
        raise ValueError("daily_at needs daily_inputs")
    if daily_at:
        try:
            hour_text, minute_text = daily_at.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except ValueError as exc:
            raise ValueError("daily_at must be HH:MM") from exc
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("daily_at is out of range")

    monitor = json.dumps(inputs or {}, ensure_ascii=False)
    command = (
        f"python main.py daemon --interval {interval} --input '{monitor}'"
    )
    if daily_at:
        daily = json.dumps(daily_inputs or {}, ensure_ascii=False)
        command += f" --daily-at {daily_at}"
        if timezone:
            command += f" --timezone {timezone}"
        command += f" --daily-input '{daily}'"
    return (
        f"Deployment command for the exported package of `{workflow_id}`:\n\n"
        f"{command}\n\n"
        "This must run on the machine that hosts the exported package, under a "
        "supervisor (systemd Restart=always, Docker --restart unless-stopped, or "
        "a Windows scheduled task). The platform schedule is the alternative when "
        "the workflow stays on this canvas; do not use both for the same job."
    )
