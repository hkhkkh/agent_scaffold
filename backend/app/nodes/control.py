"""Control-flow nodes: Loop and Variable Aggregator.

Loops deserve a note. Every platform surveyed ends up capping them, because an
agent that can loop forever eventually will:

* Dify caps a single execution path at 50 nodes,
* Flowise's ``Loop Back To`` defaults to a max loop count of 5,
* LangGraph raises ``GraphRecursionError`` at ``recursion_limit`` (default 1000).

So the Loop node has a **mandatory ceiling** (``max_iterations``) that is
enforced here, in addition to the engine-level ``recursion_limit``. The belt and
braces are intentional: this is the one place a modelling mistake costs money
instead of just time.
"""

from __future__ import annotations

from typing import Any

from app.graph.schema import (
    BooleanParam,
    DynamicOutputs,
    InputPort,
    NodeCategory,
    NodeTypeSpec,
    NumberParam,
    OutputPort,
    PortType,
    SelectOption,
    SelectParam,
    StringParam,
)
from app.nodes.base import BaseNode, NodeContext, template_scope
from app.nodes.registry import register_node
from app.runtime.expr import ExpressionError, evaluate

LOOP_KEY = "__loop__"
LOOP_BRANCH_KEY = "__branch__"

LOOP_SPEC = NodeTypeSpec(
    type="loop",
    label="Loop",
    label_zh="循环",
    category=NodeCategory.CONTROL,
    description="Repeat part of the graph until a condition is met or a ceiling is hit.",
    description_zh="重复执行图的一部分，直到条件满足或达到次数上限。",
    icon="repeat",
    color="#eab308",
    dynamic_outputs=DynamicOutputs.NONE,
    inputs=[
        InputPort(
            id="in",
            label="Value",
            label_zh="值",
            type=PortType.ANY,
            description="Carries the value being refined across iterations.",
            description_zh="在每轮之间传递被逐步改进的值。",
        )
    ],
    outputs=[
        OutputPort(
            id="continue",
            label="Continue",
            label_zh="继续",
            type=PortType.BOOLEAN,
            description="Wire this back to an earlier node to form the loop.",
            description_zh="把它接回前面的节点，就构成了循环。",
        ),
        OutputPort(
            id="done",
            label="Done",
            label_zh="结束",
            type=PortType.BOOLEAN,
            description="Wire this to whatever should run after the loop.",
            description_zh="接到循环结束后要执行的节点上。",
        ),
    ],
    params=[
        NumberParam(
            name="max_iterations",
            label="Max iterations",
            label_zh="最大循环次数",
            description="Hard ceiling. The loop always stops here, whatever the condition says.",
            description_zh="硬性上限。无论条件怎么写，到这里一定会停。",
            default=5,
            min=1,
            max=100,
            step=1,
            integer=True,
        ),
        StringParam(
            name="condition",
            label="Continue while",
            label_zh="继续循环的条件",
            description=(
                "Expression evaluated before each pass. `iteration` is the "
                "1-based pass number; earlier node outputs are available via "
                "`vars`. Examples: `iteration < 3` · "
                "`vars.judge.out.score < 0.8`"
            ),
            description_zh=(
                "每轮开始前求值的表达式。`iteration` 是轮次（从 1 开始），"
                "之前节点的输出可以通过 `vars` 访问。"
                "例如：`iteration < 3` · `vars.judge.out.score < 0.8`"
            ),
            multiline=True,
            rows=3,
            default="iteration < 3",
        ),
        SelectParam(
            name="on_max",
            label="When the ceiling is reached",
            label_zh="达到上限时",
            description="How to leave the loop when the condition never goes false.",
            description_zh="当条件一直不成立时，如何退出循环。",
            default="exit",
            options=[
                SelectOption(label="Exit normally", label_zh="正常退出", value="exit"),
                SelectOption(
                    label="Fail the run",
                    label_zh="让运行失败",
                    value="error",
                    description="Useful when hitting the ceiling means a bug.",
                    description_zh="当碰到上限意味着代码有 bug 时，用这个更好。",
                ),
            ],
        ),
        BooleanParam(
            name="expose_iteration",
            label="Pass iteration count downstream",
            label_zh="把轮次传给下游",
            description="Adds `iteration` to this node's output.",
            description_zh="在本节点的输出里加上 `iteration`。",
            default=True,
        ),
    ],
    ai_notes="""\
The shape of a loop here is not the shape most people draw first. Get it wrong
and the run either never repeats or repeats forever.

**Canonical wiring — the gate sits at the HEAD of the body, not the tail:**

    gate --continue--> [first node of the body]
    [last node of the body] --> gate          (this back edge is the loop)
    gate --done--> [whatever runs after]

Three consequences worth stating plainly:

* **`iteration` counts gate evaluations, starting at 1.** The body runs once per
  evaluation that returns true. So `iteration <= 3` runs the body exactly 3
  times. `max_iterations` is a ceiling on *evaluations*, not on body passes —
  set it at least one above the number of passes you want, or the ceiling fires
  first and the run ends a pass early.
  * Read the pass counter in a template as `{{ vars.<gate_id>.iteration }}` —
    it is a **top-level** field. `{{ vars.<gate_id>.out.iteration }}` reads the
    value the previous pass handed back, not the counter, and is almost always
    the wrong path.
* **The back edge is what makes it a loop.** Without it the gate is just a
  branch. The validator rejects a cycle that does not contain a Loop node, and
  reports a Loop node whose `continue`/`done` handles go nowhere.
* **A `continue` branch cannot fan out.** Like a Condition branch, one handle
  holds one target. To repeat several nodes, point `continue` at the body's
  first node and let that node fan out normally.

The gate forwards the value it received on its `out`, so a node can read
`{{ vars.<gate_id>.out }}` to see what the previous pass handed over. Because
each pass overwrites `vars.<node_id>`, a Code node reading `vars.<its own id>`
sees its own **previous** iteration's output — that is how a counter accumulates
across passes without needing its own state channel.\
""",
)


@register_node
class LoopNode(BaseNode):
    spec = LOOP_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        config = self.resolve_config(ctx)

        # The previous pass's output for this node survives in shared state, so
        # the counter accumulates without needing its own channel.
        previous = ctx.vars.get(ctx.node.id) or {}
        iteration = int(previous.get(LOOP_KEY, {}).get("iteration", 0)) + 1

        maximum = int(config.get("max_iterations") or 5)

        scope = template_scope(ctx, config)
        # The pass counter is only in scope for this node's expression; it is not
        # a state channel, so it is added on top rather than merged from state.
        scope["iteration"] = iteration

        expression = (config.get("condition") or "").strip()
        if not expression:
            should_continue = iteration < maximum
        else:
            try:
                should_continue = bool(evaluate(expression, scope))
            except ExpressionError as exc:
                raise ValueError(f"Loop condition is invalid: {exc}") from exc

        hit_ceiling = iteration >= maximum
        if hit_ceiling:
            should_continue = False
            if (config.get("on_max") or "exit") == "error":
                raise ValueError(
                    f"Loop reached its ceiling of {maximum} iterations without the "
                    f"condition ({expression or 'iteration < max'}) becoming false."
                )
            await ctx.log(
                f"Loop reached its ceiling of {maximum} iterations; exiting.", "warning"
            )

        branch = "continue" if should_continue else "done"
        await ctx.log(
            f"Iteration {iteration}/{maximum} -> {branch}"
            + (f" ({expression})" if expression else ""),
            "info",
        )

        result: dict[str, Any] = {
            "continue": should_continue,
            "done": not should_continue,
            LOOP_BRANCH_KEY: branch,
            LOOP_KEY: {"iteration": iteration, "max_iterations": maximum},
        }
        if config.get("expose_iteration", True):
            result["iteration"] = iteration
        # Forward the incoming value so the loop body can accumulate on it, and
        # so downstream nodes receive data rather than just a routing flag.
        if "in" in ctx.inputs:
            result["value"] = ctx.inputs["in"]
            result["out"] = ctx.inputs["in"]
        return result


# ---------------------------------------------------------------------------
# Variable Aggregator
# ---------------------------------------------------------------------------

AGGREGATOR_SPEC = NodeTypeSpec(
    type="aggregator",
    label="Variable Aggregator",
    label_zh="变量汇总",
    category=NodeCategory.CONTROL,
    description="Merge the outputs of several branches into one value.",
    description_zh="把多个分支的输出合并成一个值。",
    icon="combine",
    color="#06b6d4",
    inputs=[
        InputPort(
            id="in",
            label="Values",
            label_zh="值",
            type=PortType.ANY,
            description="Connect multiple upstream nodes here; they arrive as a list.",
            description_zh="可以把多个上游节点接到这里，它们会以列表形式传入。",
        )
    ],
    outputs=[OutputPort(id="out", label="Merged", label_zh="合并结果", type=PortType.ANY)],
    params=[
        SelectParam(
            name="mode",
            label="Merge strategy",
            label_zh="合并方式",
            default="first_non_null",
            options=[
                SelectOption(
                    label="First available value",
                    label_zh="取第一个有值的",
                    value="first_non_null",
                    description="Ideal for mutually exclusive branches — only one runs.",
                    description_zh="适合互斥分支——同一时刻只会有一个真正执行。",
                ),
                SelectOption(
                    label="Collect into a list",
                    label_zh="收集成列表",
                    value="list",
                ),
                SelectOption(
                    label="Concatenate as text",
                    label_zh="拼接成文本",
                    value="concat_text",
                ),
                SelectOption(
                    label="Merge as one object",
                    label_zh="合并成一个对象",
                    value="merge_dict",
                    description="Shallow-merge dicts; later inputs win on conflict.",
                    description_zh="浅合并字典；键冲突时后面的输入覆盖前面的。",
                ),
                SelectOption(
                    label="Only one branch ran (strict)",
                    label_zh="只允许一个分支执行（严格）",
                    value="strict_single",
                    description="Fails if more than one input is present, catching bad wiring.",
                    description_zh="如果收到多个输入就报错，可以及早发现连线接错。",
                ),
            ],
        ),
        StringParam(
            name="separator",
            label="Separator",
            label_zh="分隔符",
            description="Used by 'Concatenate as text'.",
            description_zh="「拼接成文本」时使用。",
            default="\n\n",
            advanced=True,
        ),
        StringParam(
            name="label",
            label="Output label",
            label_zh="输出标签",
            description="Optional key to attach to the merged result.",
            description_zh="可选。给合并结果附加一个键名。",
            advanced=True,
        ),
    ],
    ai_notes="""\
This is the node that joins parallel branches, and the only one that does.

* **Several edges into `in` arrive as a list; one edge arrives unwrapped.** The
  runtime only collects into a list when more than one edge lands on the handle.
  So a downstream node must cope with both shapes, or the aggregator should be
  the only consumer.
* **`label` wraps the result as `{label: merged}`.** Leave it empty when a
  downstream parameter expects a list — a Retriever given
  `{"docs": [...]}` finds no documents in it and reports "no documents",
  which reads like a fetch failure rather than a shape mistake.
* `mode: list` is what you want for merging branches. The other modes
  (`first_non_null`, `concat_text`, `merge_dict`, `strict_single`) exist for
  mutually exclusive branches and for text joins.

**Branches only merge reliably when they are the same depth.** A node is
signalled when its inputs are ready, so two branches of unequal length do not
arrive together and the aggregator can run with only part of its input. Keep
parallel branches the same number of steps long.\
""",
)


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


@register_node
class VariableAggregatorNode(BaseNode):
    spec = AGGREGATOR_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        config = self.resolve_config(ctx)

        # With several edges into `in`, the runtime supplies a list. Normalise
        # so a single connection behaves the same as many.
        raw = ctx.inputs.get("in")
        values = raw if isinstance(raw, list) else ([] if raw is None else [raw])
        available = [v for v in values if _present(v)]

        mode = config.get("mode") or "first_non_null"
        merged: Any

        if mode == "first_non_null":
            merged = available[0] if available else None
        elif mode == "list":
            merged = available
        elif mode == "concat_text":
            separator = config.get("separator") or "\n\n"
            merged = separator.join(_flat_text(v) for v in available)
        elif mode == "merge_dict":
            merged = {}
            for value in available:
                if isinstance(value, dict):
                    merged.update(value)
                else:
                    merged[config.get("label") or "value"] = value
        elif mode == "strict_single":
            if len(available) > 1:
                raise ValueError(
                    f"Variable Aggregator expected exactly one populated input but "
                    f"received {len(available)}. Branches are supposed to be mutually "
                    f"exclusive — check the upstream Condition node."
                )
            merged = available[0] if available else None
        else:
            raise ValueError(f"Unknown aggregator mode '{mode}'.")

        label = config.get("label")
        if label:
            merged = {str(label): merged}

        await ctx.log(
            f"Merged {len(available)} of {len(values)} input(s) using '{mode}'.", "info"
        )
        return {"out": merged}


def _flat_text(value: Any) -> str:
    """One value as text on **one line**, because these get joined together.

    The compact sibling of `tool._readable_text`, which indents. Indenting here
    would put newlines inside the values that `separator.join(...)` is about to
    concatenate, so the separator would stop being the only thing separating the
    inputs and the merged output would silently change shape.

    Deliberately not shared with the memory node's `_stored_text` either: that one
    renders a missing value as `""`, which is wrong for a merge — an absent input
    joined into a list is not the same as an empty string in the list.
    """
    if isinstance(value, str):
        return value
    import json

    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


__all__ = [
    "AGGREGATOR_SPEC",
    "LOOP_BRANCH_KEY",
    "LOOP_KEY",
    "LOOP_SPEC",
    "LoopNode",
    "VariableAggregatorNode",
]
