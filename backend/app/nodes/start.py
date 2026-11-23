"""Start node — the workflow's entry point.

Declares the inputs the workflow expects and forwards them to downstream nodes.
User inputs supplied at run time are merged over the declared defaults.
"""

from __future__ import annotations

from typing import Any

from app.graph.schema import (
    JsonParam,
    NodeCategory,
    NodeTypeSpec,
    OutputPort,
    PortType,
    StringParam,
)
from app.nodes.base import BaseNode, NodeContext
from app.nodes.registry import register_node

START_SPEC = NodeTypeSpec(
    type="start",
    label="Start",
    label_zh="开始",
    category=NodeCategory.INPUT,
    description="Entry point. Declares the inputs this workflow expects.",
    description_zh="入口节点。声明这个工作流需要哪些输入。",
    icon="play-circle",
    color="#22c55e",
    inputs=[],
    outputs=[
        OutputPort(
            id="out",
            label="Inputs",
            label_zh="输入",
            type=PortType.JSON,
            description="The resolved input payload for this run.",
            description_zh="本次运行最终解析出来的输入数据。",
        )
    ],
    params=[
        JsonParam(
            name="input_schema",
            label="Input fields",
            label_zh="输入字段",
            description="Keys you expect at run time, with their default values.",
            description_zh="运行时需要的字段，以及它们的默认值。",
            default={"query": ""},
            rows=7,
        ),
        StringParam(
            name="note",
            label="Note",
            label_zh="备注",
            description="Reminder for whoever reads the canvas. Not executed.",
            description_zh="给看画布的人留的提醒。不会被执行。",
            multiline=True,
            default="",
            advanced=True,
        ),
    ],
    is_entry=True,
    ai_notes="""\
The run's entry point, and the only place a graph declares what it needs from
the outside.

* **`input_schema` is the contract.** Its keys become the fields the run dialog
  offers; its values are the defaults used when the user supplies nothing. Give
  it realistic placeholder values rather than empty strings — a workflow that
  runs end to end on its defaults is one a user can try the moment it appears,
  and an empty one fails at whichever node first needs the value.
* **A graph has exactly one Start node**, and the validator enforces it. Every
  other node should be reachable from it.
* `out` is the resolved payload, so downstream nodes read fields as
  `{{ vars.<start_id>.out.<field> }}`. A Code node receives the payload itself
  on `inputs.in`.
* `note` is for a human reading the canvas and is never executed.\
""",
)


@register_node
class StartNode(BaseNode):
    spec = START_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        declared = ctx.config.get("input_schema")
        if not isinstance(declared, dict):
            declared = {}

        provided = ctx.state.get("inputs")
        if not isinstance(provided, dict):
            provided = {}

        # Declared defaults first, then whatever the user actually supplied,
        # then any extra keys so nothing is silently swallowed.
        payload: dict[str, Any] = {**declared, **provided}
        for key, value in provided.items():
            payload.setdefault(key, value)

        using_defaults = [
            key
            for key in declared
            if key not in provided or provided.get(key) in (None, "")
        ]
        suffix = (
            f"; using defaults for: {', '.join(using_defaults)}" if using_defaults else ""
        )
        await ctx.log(f"Start resolved {len(payload)} input field(s){suffix}", "info")
        return {"out": payload}


__all__ = ["START_SPEC", "StartNode"]
