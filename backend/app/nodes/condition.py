"""Condition node — deterministic branching.

Design note
-----------
This is a **real node** in the compiled graph, not a routing function folded into
its predecessor. That costs one super-step but buys two things that matter:

1. The decision is visible in the run timeline ("chose 'yes' because score > 0.8"),
   which is the single most common thing people need when debugging a branch.
2. It works no matter how many upstream nodes feed it.

The compiler then attaches ``add_conditional_edges`` to *this* node, reading the
branch it recorded in shared state.

For LLM-driven routing (where the model classifies the input), use a Condition
whose expression tests an LLM node's output — or swap in the Agent node.
"""

from __future__ import annotations

from typing import Any

from app.graph.schema import (
    DynamicOutputs,
    InputPort,
    JsonParam,
    NodeCategory,
    NodeTypeSpec,
    OutputPort,
    PortType,
    SelectOption,
    SelectParam,
)
from app.nodes.base import BaseNode, NodeContext, template_scope
from app.nodes.registry import register_node
from app.runtime.expr import ExpressionError, evaluate

#: The key under which the chosen branch id is recorded. Prefixed to make it
#: obvious in run logs that it is engine metadata, not user data.
BRANCH_KEY = "__branch__"

CONDITION_SPEC = NodeTypeSpec(
    type="condition",
    label="Condition",
    label_zh="条件分支",
    category=NodeCategory.LOGIC,
    description="Route to one of several branches by evaluating expressions.",
    description_zh="依次求值各个表达式，把流程路由到命中的那个分支。",
    icon="git-branch",
    color="#f59e0b",
    dynamic_outputs=DynamicOutputs.FROM_PARAM,
    dynamic_outputs_param="branches",
    inputs=[
        InputPort(
            id="in",
            label="Value",
            label_zh="值",
            type=PortType.ANY,
            description="Optional. Available in expressions as `inputs.in`.",
            description_zh="可选。在表达式里用 `inputs.in` 引用。",
        )
    ],
    outputs=[
        OutputPort(id="yes", label="Yes", label_zh="是", type=PortType.BOOLEAN),
        OutputPort(id="no", label="No", label_zh="否", type=PortType.BOOLEAN),
    ],
    params=[
        JsonParam(
            name="branches",
            label="Branches",
            label_zh="分支",
            description=(
                "Each branch gets its own output handle. Expressions are evaluated "
                "top to bottom and the first match wins. Leave an expression empty "
                "to make that branch the fallback."
            ),
            description_zh=(
                "每个分支会有自己的输出接口。表达式自上而下依次求值，"
                "第一个命中的生效。把表达式留空，该分支就作为兜底。"
            ),
            rows=9,
            default=[
                {"id": "yes", "label": "Yes", "expression": "len(str(inputs.in)) > 0"},
                {"id": "no", "label": "No (fallback)", "expression": ""},
            ],
        ),
        SelectParam(
            name="mode",
            label="Evaluation",
            label_zh="求值方式",
            default="first_match",
            options=[
                SelectOption(
                    label="First matching branch",
                    label_zh="取第一个命中的分支",
                    value="first_match",
                ),
                SelectOption(
                    label="Fail if nothing matches",
                    label_zh="没有命中时报错",
                    value="strict",
                    description="Useful when a fallback would hide a bug.",
                    description_zh="当兜底分支会掩盖 bug 时，用这个更好。",
                ),
            ],
        ),
    ],
    ai_notes="""\
Three wiring rules that the ports cannot show you. Each one cost a debugging
session to find, and a graph that ignores them looks correct and misbehaves.

1. **One target per branch handle.** The compiler stores a single target per
   handle, so a second edge on the same handle silently replaces the first —
   nothing warns. To run several nodes in parallel off a branch, point the
   branch at ONE plain node (a Code node works well) and give *that* node
   several outgoing edges. Fan-out is a property of ordinary nodes, not of
   branches.

2. **A branch's edges are static, and the node forwards its input.** The
   Condition node runs and returns the value it received on `out`. The runtime
   collects a node's inputs from every edge drawn to it, regardless of which
   branch was actually taken. So never wire a branch into an input handle that
   also receives real data from another node: that downstream node will be
   handed both values as a list. When a branch means "skip the rest of the
   work", send it back to the loop gate instead of into the body.

3. **Expressions see `vars`, `inputs`, and `iteration`** through the same
   evaluator the Loop node uses: comparisons, and/or/not, arithmetic,
   subscripting, attribute access on dicts, and a small set of functions
   (len, min, max, sum, sorted, abs, round, int, float, str...). No arbitrary
   calls. Guard a possibly-missing field with `x and x > 0` — `and` short
   circuits, so the second half never runs when the first is falsey.\
""",
)


@register_node
class ConditionNode(BaseNode):
    spec = CONDITION_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        branches = _normalise_branches(ctx.config.get("branches"))
        if not branches:
            raise ValueError(
                "Condition node has no branches configured. Add at least one."
            )

        config = self.resolve_config(ctx)
        scope = template_scope(ctx, config)

        chosen: str | None = None
        fallback: str | None = None
        traces: list[dict[str, Any]] = []

        for branch in branches:
            branch_id = branch["id"]
            expression = (branch.get("expression") or "").strip()

            if not expression:
                # An empty expression marks the fallback branch.
                if fallback is None:
                    fallback = branch_id
                traces.append({"branch": branch_id, "result": "fallback", "error": None})
                continue

            try:
                matched = bool(evaluate(expression, scope))
                traces.append(
                    {"branch": branch_id, "result": matched, "error": None}
                )
            except ExpressionError as exc:
                traces.append(
                    {"branch": branch_id, "result": None, "error": str(exc)}
                )
                await ctx.log(
                    f"Branch '{branch['label']}' expression failed: {exc}", "error"
                )
                raise ValueError(
                    f"Branch '{branch['label']}' has an invalid expression: {exc}"
                ) from exc

            if matched and chosen is None:
                chosen = branch_id

        mode = ctx.config.get("mode") or "first_match"
        if chosen is None:
            if fallback is not None:
                chosen = fallback
                await ctx.log(
                    f"No branch matched; taking fallback '{fallback}'.", "warning"
                )
            elif mode == "strict":
                raise ValueError(
                    "No condition branch matched and no fallback is configured."
                )
            else:
                chosen = branches[-1]["id"]
                await ctx.log(
                    f"No branch matched; defaulting to the last branch '{chosen}'.",
                    "warning",
                )

        matched_label = next(
            (b["label"] for b in branches if b["id"] == chosen), chosen
        )
        await ctx.log(f"Routed to branch '{matched_label}'.", "info")

        # Only the chosen handle is set, so downstream nodes wired to other
        # branches simply never receive a value.
        result: dict[str, Any] = {branch_id: False for branch_id in _ids(branches)}
        result[chosen] = True
        result[BRANCH_KEY] = chosen
        result["traces"] = traces
        # Forward the evaluated value so the chosen branch receives data rather
        # than just a routing flag.
        result["out"] = ctx.inputs.get("in")
        return result


def _ids(branches: list[dict[str, str]]) -> list[str]:
    return [b["id"] for b in branches]


def _normalise_branches(raw: Any) -> list[dict[str, str]]:
    """Coerce the configured branches into ``[{id, label, expression}]``."""
    if not isinstance(raw, list):
        return []

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if isinstance(item, str):
            item = {"label": item, "expression": ""}
        if not isinstance(item, dict):
            continue

        label = str(item.get("label") or item.get("name") or f"Branch {index + 1}")
        raw_id = str(item.get("id") or item.get("name") or "").strip()
        branch_id = _slug(raw_id or label) or f"branch_{index + 1}"
        if branch_id in seen:
            branch_id = f"{branch_id}_{index + 1}"
        seen.add(branch_id)

        out.append(
            {
                "id": branch_id,
                "label": label,
                "expression": str(item.get("expression") or ""),
            }
        )
    return out


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value.strip())
    return cleaned.strip("_").lower()


__all__ = ["BRANCH_KEY", "CONDITION_SPEC", "ConditionNode"]
