"""Human Input node — pause the run and wait for a person.

.. important::
   **Idempotency requirement.** LangGraph checkpoints at *super-step* boundaries,
   not inside a node. When the run resumes, this node's function is re-entered
   **from the top**. Anything you add here before :func:`interrupt` must be safe
   to run twice — write no rows, send no emails, increment no counters.

   That is why nothing but payload assembly happens before the ``interrupt()``
   call below. Preserve that property if you modify this node.

Requires a checkpointer, which the runtime configures automatically.
"""

from __future__ import annotations

from typing import Any

from app.graph.schema import (
    BooleanParam,
    InputPort,
    JsonParam,
    NodeCategory,
    NodeTypeSpec,
    OutputPort,
    PortType,
    StringParam,
)
from app.nodes.base import BaseNode, NodeContext, template_scope
from app.nodes.registry import register_node
from app.runtime.template import render

HUMAN_SPEC = NodeTypeSpec(
    type="human_input",
    label="Human Input",
    label_zh="人工确认",
    category=NodeCategory.HUMAN,
    description="Pause and wait for a person to review or supply a value.",
    description_zh="暂停运行，等人工确认或者填写内容后再继续。",
    icon="user-check",
    color="#ec4899",
    inputs=[
        InputPort(
            id="in",
            label="Context",
            label_zh="上下文",
            type=PortType.ANY,
            description="Shown to the reviewer and available to the message template.",
            description_zh="会展示给确认人，也可以在提示语模板里引用。",
        )
    ],
    outputs=[
        OutputPort(id="out", label="Response", label_zh="回复", type=PortType.JSON),
        OutputPort(
            id="approved",
            label="Approved",
            label_zh="是否通过",
            type=PortType.BOOLEAN,
            description="True when the reviewer approves; False when they reject.",
            description_zh="确认人点了「通过」为 true，点了「拒绝」为 false。",
        ),
    ],
    params=[
        StringParam(
            name="message",
            label="Message",
            label_zh="提示语",
            description="What to ask the reviewer. Supports {{ }} placeholders.",
            description_zh="要问确认人的话。支持 {{ }} 占位符。",
            multiline=True,
            rows=4,
            required=True,
            default="Please review the value above and confirm it is correct.",
        ),
        JsonParam(
            name="fields",
            label="Fields to collect",
            label_zh="需要填写的字段",
            description=(
                "Describe the inputs you want back, e.g. "
                "{\"notes\": {\"type\": \"string\", \"label\": \"Notes\"}}. "
                "A single field named 'value' gives the simplest experience."
            ),
            description_zh=(
                "描述你希望对方填什么，例如 "
                "{\"notes\": {\"type\": \"string\", \"label\": \"备注\"}}。"
                "只填一个名为 'value' 的字段时交互最简单。"
            ),
            rows=6,
            default={"value": {"type": "string", "label": "Value"}},
        ),
        BooleanParam(
            name="allow_reject",
            label="Allow rejection",
            label_zh="允许拒绝",
            description="Show a Reject button in the review dialog.",
            description_zh="在确认弹窗里显示「拒绝」按钮。",
            default=True,
        ),
        BooleanParam(
            name="show_context",
            label="Show upstream value",
            label_zh="显示上游的值",
            description="Render whatever is wired into the input handle.",
            description_zh="把接进输入接口的内容渲染出来。",
            default=True,
        ),
    ],
    ai_notes="""\
Pauses the run and asks a person, then continues with their answer. The run is
checkpointed, so it survives a backend restart while it waits.

* The run's status becomes `interrupted`, not `running`. Nothing downstream of
  this node executes until someone answers, which means a graph with a human
  node in a loop stops once per pass.
* `fields` declares what to ask for, as a JSON object of
  `{name: {"type": "string", "label": "..."}}`. The answers arrive on `out`.
  `approved` is a boolean, so a `condition` on `vars.<id>.approved` is the
  natural way to branch on a rejection.
* `allow_reject: false` for a step that is a notification rather than an
  approval — a dialog with only a Reject button reads as a trap.
* Put the decision point **after** whatever produced the thing being reviewed,
  and before anything with a side effect (a write, a send). A human node after
  the write reviews work that has already happened.
* Do not add one to a graph the user described as automatic. An unattended run
  that silently waits forever looks identical to a run that is stuck.\
""",
)


@register_node
class HumanInputNode(BaseNode):
    spec = HUMAN_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        # Deliberately deferred: importing here keeps langgraph.types out of the
        # import graph for projects that never use this node.
        from langgraph.types import interrupt

        config = self.resolve_config(ctx)

        scope = template_scope(ctx, config)

        # --- payload assembly only; no side effects before interrupt() ---
        payload = {
            "message": render(config.get("message") or "", scope).strip(),
            "fields": config.get("fields") or {},
            "allow_reject": bool(config.get("allow_reject", True)),
            "context": ctx.inputs.get("in") if config.get("show_context", True) else None,
        }

        await ctx.log("Waiting for human input...", "info")

        # Execution suspends here. On resume this function restarts and
        # interrupt() returns the value the reviewer supplied.
        response = interrupt(payload)

        if isinstance(response, dict):
            approved = bool(response.get("approved", True))
            value: Any = response.get("values", response)
        else:
            approved = True
            value = response

        await ctx.log(
            "Human input received: " + ("approved" if approved else "rejected"),
            "info" if approved else "warning",
        )
        return {"out": value, "approved": approved}


__all__ = ["HUMAN_SPEC", "HumanInputNode"]
