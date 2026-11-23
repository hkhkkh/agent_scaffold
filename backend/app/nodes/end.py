"""End node — terminal node.

Collects whatever is wired into it and records it as the run's output.
"""

from __future__ import annotations

from typing import Any

from app.graph.schema import (
    BooleanParam,
    InputPort,
    NodeCategory,
    NodeTypeSpec,
    PortType,
    StringParam,
)
from app.nodes.base import BaseNode, NodeContext
from app.nodes.registry import register_node

END_SPEC = NodeTypeSpec(
    type="end",
    label="End",
    label_zh="结束",
    category=NodeCategory.OUTPUT,
    description="Terminal node. Marks the workflow's result.",
    description_zh="终止节点。标记整个工作流的返回结果。",
    icon="stop-circle",
    color="#ef4444",
    is_terminal=True,
    inputs=[
        InputPort(
            id="in",
            label="Result",
            label_zh="结果",
            type=PortType.ANY,
            required=True,
            description="The value this workflow returns.",
            description_zh="工作流最终返回的值。",
        )
    ],
    outputs=[],
    params=[
        BooleanParam(
            name="include_state",
            label="Include full state in output",
            label_zh="在输出中包含完整状态",
            description="Useful while debugging; noisy in production.",
            description_zh="调试时有用，正式使用时会显得很啰嗦。",
            default=False,
        ),
        StringParam(
            name="output_label",
            label="Output key",
            label_zh="输出键名",
            description="Name used when reporting the run result.",
            description_zh="上报运行结果时使用的字段名。",
            default="result",
        ),
    ],
    ai_notes="""\
**Whatever is wired into this node IS the workflow's result.** There is no
separate "result" parameter to point at the right value, so the last edge is a
content decision, not a housekeeping one.

Wiring it to a node that performs a *side effect* returns that node's
acknowledgement instead of the work. A Memory node in `write` mode returns the
record's id or an empty string; a database write returns a row count. The run
then reports `{"result": "4227aab00f71d8be"}` — a successful run with a useless
deliverable, and nothing in the logs says so.

When several things are worth returning (a report, a critique, some counts),
add one node that assembles them into a single object and wire *that* to the
End node. It costs one node and it is the difference between a run whose output
can be read and one whose output has to be reconstructed from the timeline.

`include_state: true` attaches every node's output to the result as well. Useful
while debugging a graph; noisy as a deliverable.\
""",
)


@register_node
class EndNode(BaseNode):
    spec = END_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        value = ctx.input("in")
        label = ctx.config.get("output_label") or "result"

        payload: dict[str, Any] = {label: value}
        if ctx.config.get("include_state"):
            payload["state"] = {
                "vars": ctx.vars,
                "inputs": ctx.state.get("inputs") or {},
            }

        preview = repr(value)
        if len(preview) > 200:
            preview = preview[:200] + "..."
        await ctx.log(f"End accepted result for '{label}': {preview}", "info")

        # Written to vars so the runner can read it back as the final output.
        return {"__output__": payload}


__all__ = ["END_SPEC", "EndNode"]
