"""Platform actions the copilot may take beyond editing nodes.

The copilot already knows how to change a graph. These tools let it answer the
questions that decide whether the graph is *deliverable*: is it scheduled, what
are the run limits, and what command does the exported package need.

Safety rules, because these are the first tools that touch platform state:

* **Creating a schedule always creates it paused.** The copilot may be editing a
  draft that the user has not saved; an enabled schedule would fire the previous
  saved definition. The tool result says, in plain words, that the user must save
  and then enable it.
* **Public demo and scheduler-disabled deployments cannot change schedules.**
  Those are the same flags the API enforces, read at call time.
* **No schedule is ever deleted here.** Deleting a trigger is a destructive act
  the user can perform in the API/UI; an AI should not remove an operational
  object because a sentence sounded like it wanted a different cadence.
"""

from __future__ import annotations

from typing import Any

from app.ai_buildtime.platform_brief import (
    deployment_plan,
    platform_brief,
    workflow_context,
)
from app.scheduling.rules import ScheduleRule, ScheduleRuleError
from app.settings import get_settings


def platform_tool_specs() -> list[dict[str, Any]]:
    """OpenAI function schemas for the platform tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": "get_platform_context",
                "description": (
                    "Read this platform's live run limits, schedule vocabulary, "
                    "export/daemon facts, and the triggers currently registered "
                    "for this workflow. Call this before answering anything about "
                    "continuous operation, scheduling, deployment or limits."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_schedule",
                "description": (
                    "Create a time trigger for this workflow, in a PAUSED state. "
                    "Only for a saved workflow. After creating it, tell the user "
                    "to save the graph and enable the trigger. Do not claim it is "
                    "running."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "frequency": {
                            "type": "string",
                            "enum": [
                                "minutely",
                                "hourly",
                                "daily",
                                "weekly",
                                "monthly",
                            ],
                        },
                        "interval": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Every N frequency units. Default 1.",
                        },
                        "minute": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 59,
                            "description": "Minute of the hour, for hourly/daily/weekly/monthly.",
                        },
                        "hour": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0, "maximum": 23},
                            "description": "Hours to fire (UTC), for daily/weekly/monthly.",
                        },
                        "weekday": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "mon",
                                    "tue",
                                    "wed",
                                    "thu",
                                    "fri",
                                    "sat",
                                    "sun",
                                ],
                            },
                        },
                        "day": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 28,
                            "description": "Day of month, for monthly.",
                        },
                        "name": {"type": "string"},
                        "inputs": {
                            "type": "object",
                            "description": (
                                "Inputs for each fire. Must match the Start "
                                "node's input_schema."
                            ),
                        },
                    },
                    "required": ["frequency"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_schedule_enabled",
                "description": (
                    "Pause or enable an existing trigger of THIS workflow. Only "
                    "enable after the user has saved the graph and explicitly "
                    "asked for it to run."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schedule_id": {"type": "string"},
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["schedule_id", "enabled"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "deployment_plan",
                "description": (
                    "Return the exact `main.py daemon` command for exporting this "
                    "workflow to a packaged agent. Does not export or run anything."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "interval": {"type": "integer", "minimum": 1},
                        "daily_at": {
                            "type": "string",
                            "description": "HH:MM local time; empty disables the daily run.",
                        },
                        "timezone": {
                            "type": "string",
                            "description": "IANA zone, e.g. Asia/Shanghai.",
                        },
                        "inputs": {"type": "object"},
                        "daily_inputs": {"type": "object"},
                    },
                },
            },
        },
    ]


def _check_workflow(workflow_id: str) -> Any:
    from app.db.repository import get_workflow

    try:
        return get_workflow(workflow_id)
    except Exception as exc:  # noqa: BLE001 - the message is the answer
        raise ValueError(
            f"workflow '{workflow_id}' is not saved yet; save it before scheduling"
        ) from exc


def _check_inputs(graph: Any, inputs: dict[str, Any]) -> None:
    """Catch obvious typos before the first fire fails silently."""
    start = next((node for node in graph.nodes if node.type == "start"), None)
    schema = (start.config or {}).get("input_schema") if start else None
    if isinstance(schema, dict) and schema:
        unknown = sorted(set(inputs) - set(schema))
        if unknown:
            raise ValueError(
                "these inputs are not in the Start input_schema: "
                + ", ".join(unknown)
            )


def _schedule_line(row: dict[str, Any]) -> str:
    return (
        f"{row.get('id')} | enabled={row.get('enabled')} | "
        f"status={row.get('status')} | rule={row.get('rule')} | "
        f"next={row.get('next_run_at') or '-'}"
    )


def create_schedule(
    workflow_id: str,
    *,
    frequency: str,
    interval: int = 1,
    minute: int | None = None,
    hour: list[int] | None = None,
    weekday: list[str] | None = None,
    day: int | None = None,
    name: str = "",
    inputs: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    if settings.public_demo:
        raise ValueError("scheduling is disabled in the public demo")
    if not settings.scheduler_enabled:
        raise ValueError(
            "the scheduler is disabled in this deployment (SCHEDULER_ENABLED=false)"
        )

    graph = _check_workflow(workflow_id)
    payload = {
        "frequency": frequency,
        "interval": int(interval or 1),
        "minute": minute,
        "hour": list(hour or []),
        "weekday": list(weekday or []),
        "day": day,
    }
    try:
        rule = ScheduleRule.from_dict(payload)
    except ScheduleRuleError as exc:
        raise ValueError(f"invalid schedule rule: {exc}") from exc

    values = dict(inputs or {})
    _check_inputs(graph, values)

    from app.scheduling import store
    from app.scheduling.service import get_scheduler

    row = store.create_schedule(
        workflow_id=workflow_id,
        rule=rule.to_dict(),
        name=name or f"{graph.name} — {frequency}",
        inputs=values,
        # Always paused: the copilot cannot know whether the canvas has unsaved
        # edits, and firing the previous definition would be a silent surprise.
        enabled=False,
    )
    get_scheduler().sync()
    return (
        "Created a PAUSED trigger. It will not fire yet.\n"
        f"{_schedule_line(row)}\n"
        "Tell the user to save the workflow, then enable this trigger. If they "
        "asked for it to run now, they must confirm the saved graph is the one "
        "they want; enabling is a separate, explicit step."
    )


def set_schedule_enabled(
    workflow_id: str, schedule_id: str, enabled: bool
) -> str:
    settings = get_settings()
    if settings.public_demo:
        raise ValueError("scheduling is disabled in the public demo")
    if enabled and not settings.scheduler_enabled:
        raise ValueError(
            "the scheduler is disabled in this deployment (SCHEDULER_ENABLED=false)"
        )

    from app.scheduling import store
    from app.scheduling.service import get_scheduler

    row = store.get_schedule(schedule_id)
    if row is None:
        raise ValueError(f"no trigger with id '{schedule_id}'")
    if row.get("workflow_id") != workflow_id:
        raise ValueError(
            "that trigger belongs to a different workflow; refusing to change it"
        )
    changed = store.update_schedule(schedule_id, enabled=bool(enabled))
    get_scheduler().sync()
    return (
        f"Trigger {'enabled' if enabled else 'paused'}.\n"
        f"{_schedule_line(changed)}"
    )


def dispatch_platform_tool(
    name: str, args: dict[str, Any], workflow_id: str
) -> str | None:
    """Run a platform tool, or return ``None`` when it is not one of ours."""
    try:
        if name == "get_platform_context":
            return workflow_context(workflow_id)
        if name == "create_schedule":
            return create_schedule(
                workflow_id,
                frequency=str(args.get("frequency") or ""),
                interval=int(args.get("interval") or 1),
                minute=args.get("minute"),
                hour=args.get("hour") or [],
                weekday=args.get("weekday") or [],
                day=args.get("day"),
                name=str(args.get("name") or ""),
                inputs=args.get("inputs") or {},
            )
        if name == "set_schedule_enabled":
            return set_schedule_enabled(
                workflow_id,
                str(args.get("schedule_id") or ""),
                bool(args.get("enabled")),
            )
        if name == "deployment_plan":
            return deployment_plan(
                workflow_id,
                interval=int(args.get("interval") or 60),
                daily_at=str(args.get("daily_at") or ""),
                timezone=str(args.get("timezone") or ""),
                inputs=args.get("inputs") or {},
                daily_inputs=args.get("daily_inputs"),
            )
    except Exception as exc:  # noqa: BLE001 - tool errors are data for the model
        return f"ERROR: {exc}"
    return None


def capability_tool_names() -> set[str]:
    return {spec["function"]["name"] for spec in platform_tool_specs()}


__all__ = [
    "capability_tool_names",
    "create_schedule",
    "deployment_plan",
    "dispatch_platform_tool",
    "platform_brief",
    "platform_tool_specs",
    "set_schedule_enabled",
    "workflow_context",
]
