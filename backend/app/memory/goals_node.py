"""Goal node — start a plan, advance it, or report where it stands.

One node type with a `mode`, for the same reason the memory node has one: the
difference between the operations is *which* call it makes on one record, not what
it is. Four types would mean four specs and four sets of parameters describing one
object.

Why a node and not a middleware
-------------------------------
Upstream's `TodoListMiddleware` is an excellent design that cannot be taken whole:
its hooks are `before_model` / `wrap_model_call`, driven by `create_agent`'s loop,
and our nodes do not use `create_agent` (see `docs/target/reuse.md` §3). What is
taken is the valuable half — the model-facing wording, which is imported from that
module rather than paraphrased, so an upstream improvement arrives for free.

The modes
---------
* `start` — write a new plan. Writes a new record; does not touch an existing one.
* `advance` — move an item to `completed`, or reopen one. **This is the mode the
  agent loop uses**, because a plan that is never updated is a plan that lies.
* `show` — read the current goal back, including which item is next. What a
  downstream prompt or the canvas reads.

`start` and `advance` are separate modes rather than one upsert because resuming
a goal and replacing it are different intentions, and a single call that could do
either would occasionally do the wrong one.
"""

from __future__ import annotations

import json
from typing import Any

from app.graph.schema import (
    InputPort,
    JsonParam,
    NodeCategory,
    NodeTypeSpec,
    OutputPort,
    PortType,
    SelectOption,
    SelectParam,
    StringParam,
)
from app.memory.goals import (
    GOALS_SEGMENT,
    GOAL_ABANDONED,
    GOAL_ACTIVE,
    GOAL_DONE,
    GoalError,
    build_goal,
    is_finished,
    next_item,
    normalize_items,
    progress,
    reconcile_status,
    summarize,
)
from app.nodes.base import (
    BaseNode,
    NodeContext,
    current_run_id,
    render_template,
)
from app.nodes.registry import register_node

MODE_START = "start"
MODE_ADVANCE = "advance"
MODE_SHOW = "show"

#: How many goals `show` returns when listing a scope rather than one goal.
DEFAULT_LIMIT = 10
MAX_LIMIT = 100


def build_goal_spec() -> NodeTypeSpec:
    return NodeTypeSpec(
        type="goal",
        label="Goal",
        label_zh="目标",
        category=NodeCategory.DATA,
        description=(
            "Track a multi-step objective: write the plan, mark steps done as "
            "you go, and read back what is left. The goal outlives the run, so a "
            "later run picks the plan up where it stopped."
        ),
        description_zh=(
            "跟踪一个多步骤的目标：写下计划、逐步标记完成、读回还剩什么。"
            "目标比本次运行活得久，所以之后的运行能接着上次停下的地方继续。"
        ),
        icon="list-checks",
        color="#0d9488",
        inputs=[
            InputPort(
                id="in",
                label="Input",
                label_zh="输入",
                type=PortType.ANY,
                description="Used when the objective field is empty.",
                description_zh="当目标描述字段为空时使用。",
            )
        ],
        outputs=[
            OutputPort(
                id="out",
                label="Goal",
                label_zh="目标",
                type=PortType.ANY,
                description=(
                    "The goal record: id, objective, status, items, progress."
                ),
                description_zh="目标记录：id、描述、状态、条目、进度。",
            ),
            OutputPort(
                id="next",
                label="Next step",
                label_zh="下一步",
                type=PortType.TEXT,
                description="What to work on now, or empty when the plan is done.",
                description_zh="现在该做的那一步；计划做完时为空。",
            ),
        ],
        params=[
            SelectParam(
                name="mode",
                label="Mode",
                label_zh="模式",
                description="Write a plan, advance it, or read it back.",
                description_zh="写下计划、推进计划，或读回计划。",
                default=MODE_SHOW,
                options=[
                    SelectOption(
                        value=MODE_SHOW,
                        label="Show the goal",
                        label_zh="读回目标",
                        description="Needs no update. What the loop reads.",
                        description_zh="不修改任何东西。循环读的就是它。",
                    ),
                    SelectOption(
                        value=MODE_START,
                        label="Start a plan",
                        label_zh="开始一个计划",
                        description="Writes a new goal. Leaves an existing one alone.",
                        description_zh="写一个新的目标，不动已有的那个。",
                    ),
                    SelectOption(
                        value=MODE_ADVANCE,
                        label="Advance the plan",
                        label_zh="推进计划",
                        description="Mark an item done, or reopen it.",
                        description_zh="把某个条目标记完成，或重新打开。",
                    ),
                ],
            ),
            StringParam(
                name="objective",
                label="Objective",
                label_zh="目标描述",
                description=(
                    "Start mode only. One sentence saying what is being done. "
                    "Leave empty to use the node's input."
                ),
                description_zh=(
                    "仅开始模式。一句话说明要做什么。留空则用本节点的输入。"
                ),
            ),
            JsonParam(
                name="items",
                label="Plan steps",
                label_zh="计划步骤",
                description=(
                    "Start mode only. A list of strings, or of "
                    '{"content": "...", "status": "pending"}. Status defaults '
                    "to pending."
                ),
                description_zh=(
                    "仅开始模式。可以是字符串数组，或 "
                    '{"content": "...", "status": "pending"} 的数组。'
                    "状态默认 pending。"
                ),
                default=[],
            ),
            StringParam(
                name="scope",
                label="Scope",
                label_zh="作用域",
                description=(
                    "Which goals this node sees. `{agent}` is this workflow, "
                    "`{run}` the current run. Use the same value to read what "
                    "you wrote."
                ),
                description_zh=(
                    "本节点看到哪一批目标。`{agent}` 是本工作流，`{run}` 是本次"
                    "运行。读写用同一个值。"
                ),
                default="{agent}",
                placeholder="{agent}",
            ),
            StringParam(
                name="item",
                label="Step",
                label_zh="条目",
                description=(
                    "Advance mode only. The step to change — by its text, or its "
                    "1-based number. Empty advances the current step."
                ),
                description_zh=(
                    "仅推进模式。要改的那一步 —— 用它的文本，或它的序号（从 1 起）。"
                    "留空则推进当前那一步。"
                ),
            ),
            SelectParam(
                name="item_status",
                label="Set it to",
                label_zh="设为",
                description="Advance mode only.",
                description_zh="仅推进模式。",
                default="completed",
                options=[
                    SelectOption(
                        value="completed",
                        label="Done",
                        label_zh="已完成",
                        description="Default. Marks the step finished.",
                        description_zh="默认。把这一步标记为完成。",
                    ),
                    SelectOption(
                        value="in_progress",
                        label="In progress",
                        label_zh="进行中",
                        description="Started but not finished. Only one step should be here.",
                        description_zh="已开始但没做完。同一时间只应有一个。",
                    ),
                    SelectOption(
                        value="pending",
                        label="Not started",
                        label_zh="未开始",
                        description="Reopens a step that was closed too early.",
                        description_zh="把过早关闭的步骤重新打开。",
                    ),
                ],
            ),
            SelectParam(
                name="goal_status",
                label="Goal status",
                label_zh="目标状态",
                description=(
                    "Advance mode only. Normally left alone: completing the last "
                    "step marks the goal done automatically."
                ),
                description_zh=(
                    "仅推进模式。通常不用改：最后一步完成时目标会自动标记完成。"
                ),
                default="",
                options=[
                    SelectOption(
                        value="",
                        label="Leave it derived",
                        label_zh="保持不变",
                        description="Recommended. Status follows the steps.",
                        description_zh="推荐。状态跟着步骤走。",
                    ),
                    SelectOption(
                        value=GOAL_ABANDONED,
                        label="Abandon",
                        label_zh="放弃",
                        description="Stop working on it without finishing it.",
                        description_zh="不再做它了，但也没做完。",
                    ),
                    SelectOption(
                        value=GOAL_ACTIVE,
                        label="Reopen",
                        label_zh="重新开始",
                        description="Undo a completion.",
                        description_zh="撤销「已完成」。",
                    ),
                ],
            ),
            StringParam(
                name="goal_id",
                label="Goal id",
                label_zh="目标 id",
                description=(
                    "Show and advance modes. Leave empty to use the most recent "
                    "goal in the scope, which is what a loop wants."
                ),
                description_zh=(
                    "读回与推进模式。留空则用作用域里最近的那个目标 —— "
                    "循环通常就要这个。"
                ),
            ),
            JsonParam(
                name="limit",
                label="How many",
                label_zh="返回条数",
                description="Show mode only, when listing a scope.",
                description_zh="仅读回模式，且是列出一个作用域时。",
                default=DEFAULT_LIMIT,
                advanced=True,
            ),
        ],
    )


def _namespace(scope: str, workflow_id: str, run_id: str) -> tuple[str, ...]:
    """`<scope>/goals`. The fixed segment keeps goals out of memory search."""
    text = (scope or "{agent}").strip()
    parts = [
        segment.strip()
        for segment in text.format(agent=workflow_id or "agent", run=run_id or "run").split("/")
    ]
    head = tuple(part for part in parts if part) or ("agent",)
    return (*head, GOALS_SEGMENT)


@register_node
class GoalNode(BaseNode):
    spec = build_goal_spec()

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        from app.memory.store import get_memory_store

        store = get_memory_store()
        config = self.resolve_config(ctx)
        mode = str(config.get("mode") or MODE_SHOW)

        if store is None:
            # `MEMORY_STORE=off` is a supported deployment, so a graph with a goal
            # node still has to run on it. Same reasoning as the memory node.
            await ctx.log(
                "Goals need a store and MEMORY_STORE=off, so this node did nothing.",
                "warn",
            )
            return {"out": None, "next": ""}

        run_id = current_run_id()
        namespace = _namespace(
            str(config.get("scope") or "{agent}"),
            str(ctx.state.get("workflow_id") or ""),
            run_id,
        )

        if mode == MODE_START:
            return await self._start(ctx, store, namespace, config, run_id)
        if mode == MODE_ADVANCE:
            return await self._advance(ctx, store, namespace, config)
        return await self._show(ctx, store, namespace, config)

    # -- start ---------------------------------------------------------------

    async def _start(
        self,
        ctx: NodeContext,
        store: Any,
        namespace: tuple[str, ...],
        config: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        objective = render_template(str(config.get("objective") or ""), ctx, config).strip()
        if not objective:
            objective = str(ctx.inputs.get("in") or "").strip()
        if isinstance(config.get("items"), str):
            # `{{ }}` in a JSON field arrives as text; render before parsing so a
            # plan can be built from an upstream value.
            items: Any = render_template(config["items"], ctx, config)
        else:
            items = config.get("items")

        try:
            goal = build_goal(
                objective,
                items,
                run_id=run_id,
                node_id=str(ctx.node.id or ""),
            )
        except GoalError as exc:
            # A goal that cannot be built is the author's mistake, and the run
            # should say so rather than continue with no plan while appearing to
            # have one. `on_error` on the node is what makes that survivable.
            raise ValueError(f"Goal node could not start a plan: {exc}") from exc

        await store.aput(namespace, goal["id"], goal)
        await ctx.log(f"Started {summarize(goal)}", "info")
        return {"out": goal, "next": (next_item(goal) or {}).get("content", "")}

    # -- advance -------------------------------------------------------------

    async def _advance(
        self,
        ctx: NodeContext,
        store: Any,
        namespace: tuple[str, ...],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        goal = await self._load(ctx, store, namespace, config)
        if goal is None:
            await ctx.log(
                "No goal to advance. Run this node in `start` mode first.", "warn"
            )
            return {"out": None, "next": ""}

        wanted = render_template(str(config.get("item") or ""), ctx, config).strip()
        target = self._find_item(goal, wanted)
        if target is None:
            await ctx.log(f"No step matching {wanted!r} in '{goal['objective']}'.", "warn")
            return {"out": goal, "next": (next_item(goal) or {}).get("content", "")}

        new_status = str(config.get("item_status") or "completed")
        target["status"] = new_status

        explicit = str(config.get("goal_status") or "").strip()
        if explicit:
            goal["status"] = explicit
        reconcile_status(goal)

        await store.aput(namespace, goal["id"], goal)
        await ctx.log(
            f"Step {new_status}: {target['content'][:80]} — now {summarize(goal)}", "info"
        )
        upcoming = next_item(goal)
        return {
            "out": goal,
            "next": (upcoming or {}).get("content", ""),
        }

    @staticmethod
    def _find_item(goal: dict[str, Any], wanted: str) -> dict[str, Any] | None:
        """Find the step to change, by number or by text.

        Number first: a plan whose steps read "1. check the log" would otherwise
        match the text `1` against the wrong item. A 1-based index because that is
        how the plan is shown.
        """
        items = goal.get("items") or []
        if not wanted:
            return next_item(goal)
        if wanted.isdigit():
            index = int(wanted) - 1
            if 0 <= index < len(items):
                return items[index]
            return None
        for item in items:
            if (item or {}).get("content") == wanted:
                return item
        # A prefix match, because a model repeating a long step rarely reproduces
        # it exactly. First match wins, so the order of the plan decides ties.
        for item in items:
            if wanted and str(item.get("content", "")).startswith(wanted):
                return item
        return None

    # -- show ----------------------------------------------------------------

    async def _show(
        self,
        ctx: NodeContext,
        store: Any,
        namespace: tuple[str, ...],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        goal = await self._load(ctx, store, namespace, config)
        if goal is None:
            await ctx.log("No goal in this scope yet.", "info")
            return {"out": None, "next": ""}

        counts = progress(goal)
        upcoming = next_item(goal)
        if is_finished(goal):
            await ctx.log(f"{summarize(goal)} — every step is done.", "info")
        else:
            await ctx.log(
                f"{summarize(goal)} — next: {(upcoming or {}).get('content', '?')}", "info"
            )
        return {
            "out": goal,
            "next": (upcoming or {}).get("content", ""),
            "progress": counts,
        }

    # -- shared --------------------------------------------------------------

    @staticmethod
    async def _load(
        ctx: NodeContext,
        store: Any,
        namespace: tuple[str, ...],
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        """One goal by id, or the most recent in the scope.

        "Most recent" rather than "the only one" because a scope accumulates goals
        across runs, and the useful default for a loop is the one it is working on.
        Sorted by `updated_at`, not `created_at`, so a goal resumed yesterday beats
        one started this morning and abandoned.
        """
        goal_id = render_template(str(config.get("goal_id") or ""), ctx, config).strip()
        if goal_id:
            item = await store.aget(namespace, goal_id)
            return dict(item.value) if item is not None else None

        # Only when listing. `asearch` cannot order by a value field, so the sort
        # happens here over a bounded read.
        items = await store.asearch(namespace, limit=MAX_LIMIT)
        if not items:
            return None
        goals = [dict(item.value) for item in items]
        return max(goals, key=lambda g: float(g.get("updated_at") or 0))


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MODE_ADVANCE",
    "MODE_SHOW",
    "MODE_START",
    "GoalNode",
    "build_goal_spec",
]
