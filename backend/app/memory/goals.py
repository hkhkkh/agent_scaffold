"""Goals: what an agent is working on, and how far it has got.

Why this is not the memory store
--------------------------------
Both persist, and it is tempting to put them in one place. They differ in the
thing that matters:

* **Memory** is a collection of facts. Its questions are "what do I know about
  this customer" and the answer is a ranked list. Order does not matter, and an
  item's value does not change.
* **A goal** is a single object with a lifecycle. Its questions are "what am I
  doing", "what is next", "is this finished", and the answer is one record whose
  *status fields change over time*.

Searching for a goal semantically is the wrong operation, and mutating a fact is
the wrong operation on a memory. Sharing a store would mean one of them carrying
a schema it does not use.

What is reused rather than written
----------------------------------
* **The item shape and the three statuses** are `langchain.agents.middleware.todo`'s
  `Todo`, imported rather than re-declared. It is a `TypedDict` with `content` and
  `status: Literal["pending", "in_progress", "completed"]`.
* **The wording that tells a model how to behave** is upstream's
  `WRITE_TODOS_SYSTEM_PROMPT` and `WRITE_TODOS_TOOL_DESCRIPTION`. Those are tuned
  text, not boilerplate, and the parts that took iteration are in them: when *not*
  to use the list, "do not batch completions", "always at least one in_progress".
  Imported so an upstream improvement arrives for free — re-typing them would make
  them a copy that drifts.

What is ours
------------
The **storage**, because upstream's version lives in the graph state of one
`create_agent` loop. A goal here has to outlive a run — that is the entire point
of task 21 — and it has to be readable by the canvas. So it is a row, not a state
channel.

Storage choice: **the same file as memory.** `MEMORY_STORE` already names a
location that is opened in the lifespan and closed at shutdown, and a goal is the
same kind of thing. A second setting would let a deployment configure memory and
silently get no goals.

Layout: one record per goal, keyed by the goal id, in a namespace
`<scope>/goals`. The *item list* lives inside the record's value, because the
items are only meaningful together — a reader always wants the whole plan, and
splitting them into rows would mean reassembling it on every read.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from langchain.agents.middleware.todo import Todo as TodoItem

#: Re-exported so callers do not reach into a middleware module for a data shape.
#: Upstream's `Todo` is a `TypedDict`, so this is a plain alias.
Todo = TodoItem

#: The three statuses, taken from upstream's `Literal` rather than re-typed.
TodoStatus = Literal["pending", "in_progress", "completed"]

#: Goal-level status. Deliberately **not** the item statuses: a goal can be
#: abandoned, which is not something an individual item needs to express, and
#: an item can be `in_progress` inside a goal that is `blocked`.
GOAL_ACTIVE = "active"
GOAL_DONE = "done"
GOAL_ABANDONED = "abandoned"
GOAL_STATUSES = (GOAL_ACTIVE, GOAL_DONE, GOAL_ABANDONED)

#: Where a goal's record sits under a scope. A fixed segment so a scope can hold
#: goals and memory without either search seeing the other.
GOALS_SEGMENT = "goals"

#: Longest objective text accepted. A goal is a sentence, not a document.
MAX_OBJECTIVE_CHARS = 2_000
#: Longest item text. Same reasoning, one line each.
MAX_ITEM_CHARS = 2_000
#: How many items one goal may carry. A plan with hundreds of steps is not a plan,
#: and an unbounded list is an unbounded prompt.
MAX_ITEMS = 200


class GoalError(ValueError):
    """Raised when a goal cannot be accepted as written."""


# ---------------------------------------------------------------------------
# Building and validating
# ---------------------------------------------------------------------------


def validate_objective(objective: str) -> str:
    """Check the goal's one-line description. Returns it, for inline use."""
    text = (objective or "").strip()
    if not text:
        raise GoalError("A goal needs an objective: one sentence saying what is being done.")
    if len(text) > MAX_OBJECTIVE_CHARS:
        raise GoalError(
            f"The objective is {len(text)} characters; the limit is "
            f"{MAX_OBJECTIVE_CHARS}. A goal is a sentence, not a document — put "
            "the detail in the items."
        )
    return text


def normalize_items(items: Any) -> list[dict[str, str]]:
    """Coerce a list of items into stored form, refusing what cannot be stored.

    Accepts what a model actually produces: plain strings, `{"content": ...}`
    dicts, or upstream `Todo` dicts. Refusing the string form would push the same
    coercion into every caller, and the model will produce it whatever the schema
    says.
    """
    if items in (None, ""):
        return []
    if isinstance(items, str):
        # A single item, or a newline-separated list. The second is what a model
        # writes when it is asked for "the steps" and answers in prose.
        items = [line for line in items.splitlines() if line.strip()]
    if not isinstance(items, (list, tuple)):
        raise GoalError(
            f"Items must be a list, not {type(items).__name__}. "
            'Each item is {"content": "...", "status": "pending"}.'
        )
    if len(items) > MAX_ITEMS:
        raise GoalError(
            f"{len(items)} items exceeds the limit of {MAX_ITEMS}. A plan with "
            "that many steps is not a plan; break it into goals."
        )

    out: list[dict[str, str]] = []
    for index, raw in enumerate(items):
        if isinstance(raw, str):
            content, status = raw, "pending"
        elif isinstance(raw, dict):
            content = raw.get("content") or raw.get("text") or raw.get("task") or ""
            status = raw.get("status") or "pending"
        else:
            raise GoalError(
                f"Item {index} is a {type(raw).__name__}; expected a string or "
                '{"content": "...", "status": "..."}.'
            )

        content = str(content).strip()
        if not content:
            raise GoalError(f"Item {index} has no content.")
        if len(content) > MAX_ITEM_CHARS:
            raise GoalError(
                f"Item {index} is {len(content)} characters; the limit is "
                f"{MAX_ITEM_CHARS}."
            )
        # An unrecognised status becomes `pending` rather than raising. The model
        # invents statuses ("done", "todo", "in progress"), and failing the whole
        # goal over one is a worse outcome than showing it as not-yet-started —
        # which is the safe reading, since it will be revisited.
        if status not in ("pending", "in_progress", "completed"):
            status = "pending"
        out.append({"content": content, "status": status})
    return out


def new_goal_id() -> str:
    """A short, sortable-enough id. Not a uuid4 because it appears in a URL."""
    return uuid.uuid4().hex[:12]


def build_goal(
    objective: str,
    items: Any = None,
    *,
    goal_id: str | None = None,
    run_id: str = "",
    node_id: str = "",
) -> dict[str, Any]:
    """A complete goal record, ready to store.

    `created_at` is a float rather than a formatted string so a reader can sort
    without parsing, and so two goals created in the same second still order
    correctly.
    """
    return {
        "id": goal_id or new_goal_id(),
        "objective": validate_objective(objective),
        "status": GOAL_ACTIVE,
        "items": normalize_items(items),
        "created_at": time.time(),
        "updated_at": time.time(),
        "run_id": str(run_id or ""),
        "node_id": str(node_id or ""),
    }


# ---------------------------------------------------------------------------
# Reading a goal's progress
# ---------------------------------------------------------------------------


def progress(goal: dict[str, Any]) -> dict[str, int]:
    """Counts by status, plus the total. What a progress bar renders."""
    items = goal.get("items") or []
    counts = {"pending": 0, "in_progress": 0, "completed": 0}
    for item in items:
        status = (item or {}).get("status")
        if status in counts:
            counts[status] += 1
    counts["total"] = len(items)
    return counts


def next_item(goal: dict[str, Any]) -> dict[str, str] | None:
    """The item to work on now, or `None` when there is nothing left.

    An `in_progress` item wins over a `pending` one, because a plan that has been
    worked on should be resumed rather than restarted. Otherwise the first pending
    item — in listed order, since that order is the plan.
    """
    items = goal.get("items") or []
    for item in items:
        if (item or {}).get("status") == "in_progress":
            return item
    for item in items:
        if (item or {}).get("status") == "pending":
            return item
    return None


def is_finished(goal: dict[str, Any]) -> bool:
    """Whether every item is done.

    An empty plan is **not** finished: a goal with no steps either has not been
    planned yet or was created by mistake, and calling either "done" would let a
    run declare success without doing anything.
    """
    items = goal.get("items") or []
    return bool(items) and all(
        (item or {}).get("status") == "completed" for item in items
    )


def reconcile_status(goal: dict[str, Any]) -> dict[str, Any]:
    """Bring the goal's own status in line with its items.

    Called after an update rather than by the caller, so "the last item completed"
    and "the goal is done" cannot disagree — which is the state a reader would see
    as a stuck goal reporting 100%.
    """
    if is_finished(goal):
        goal["status"] = GOAL_DONE
    elif goal.get("status") == GOAL_DONE:
        # Something was reopened. Going back to `active` rather than staying
        # `done` is the honest reading: the plan has work in it again.
        goal["status"] = GOAL_ACTIVE
    goal["updated_at"] = time.time()
    return goal


def summarize(goal: dict[str, Any]) -> str:
    """One line for a log or a list. Not localised — it is a log line."""
    counts = progress(goal)
    return (
        f"[{goal.get('status', '?')}] {goal.get('objective', '')} "
        f"({counts['completed']}/{counts['total']} done)"
    )


__all__ = [
    "GOALS_SEGMENT",
    "GOAL_ACTIVE",
    "GOAL_ABANDONED",
    "GOAL_DONE",
    "GOAL_STATUSES",
    "MAX_ITEMS",
    "MAX_ITEM_CHARS",
    "MAX_OBJECTIVE_CHARS",
    "GoalError",
    "Todo",
    "TodoStatus",
    "build_goal",
    "is_finished",
    "new_goal_id",
    "next_item",
    "normalize_items",
    "progress",
    "reconcile_status",
    "summarize",
    "validate_objective",
]
