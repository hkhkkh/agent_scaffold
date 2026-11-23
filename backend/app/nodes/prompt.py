"""Prompt node — pure templating, no model call.

Kept separate from the LLM node on purpose: composing a string and spending
tokens are different concerns, and separating them makes a workflow cheaper to
inspect (you can see the exact prompt that was sent) and easier to reuse across
several model nodes.
"""

from __future__ import annotations

from typing import Any

from app.graph.schema import (
    CodeParam,
    InputPort,
    NodeCategory,
    NodeTypeSpec,
    OutputPort,
    PortType,
    SelectOption,
    SelectParam,
    StringParam,
)
from app.nodes.base import BaseNode, NodeContext, template_scope
from app.nodes.registry import register_node
from app.runtime.template import render

PROMPT_SPEC = NodeTypeSpec(
    type="prompt",
    label="Prompt Template",
    label_zh="提示词模板",
    category=NodeCategory.DATA,
    description="Compose a string from the graph's state. Sends nothing.",
    description_zh="根据图里的数据拼出一段文本。不会调用任何模型。",
    icon="file-text",
    color="#0ea5e9",
    inputs=[
        InputPort(
            id="in",
            label="Values",
            label_zh="值",
            type=PortType.ANY,
            description="Optional. Exposes the upstream value as {{ inputs.in }}.",
            description_zh="可选。上游的值会以 {{ inputs.in }} 的形式可用。",
        )
    ],
    outputs=[
        OutputPort(id="out", label="Text", label_zh="文本", type=PortType.TEXT),
        OutputPort(id="vars", label="Variables", label_zh="变量", type=PortType.JSON),
    ],
    params=[
        CodeParam(
            name="template",
            label="Template",
            label_zh="模板",
            description=(
                "Use {{ }} placeholders. Examples: {{ query }} · "
                "{{ inputs.in }} · {{ vars.node_1.out }}"
            ),
            description_zh=(
                "用 {{ }} 占位符。例如：{{ query }} · "
                "{{ inputs.in }} · {{ vars.node_1.out }}"
            ),
            language="jinja2",
            rows=9,
            required=True,
            default="{{ query }}",
        ),
        StringParam(
            name="separator",
            label="Join separator",
            label_zh="连接分隔符",
            description="Used when the incoming value is a list.",
            description_zh="当传入的值是列表时，用它把各项连接起来。",
            default="\n\n",
            advanced=True,
        ),
        SelectParam(
            name="missing",
            label="Missing placeholders",
            label_zh="占位符无法解析时",
            description="What to do when a placeholder cannot be resolved.",
            description_zh="当某个占位符取不到值时该怎么办。",
            default="empty",
            options=[
                SelectOption(label="Render as empty text", label_zh="渲染为空文本", value="empty"),
                SelectOption(label="Fail the node", label_zh="让节点失败", value="error"),
            ],
        ),
    ],
    ai_notes="""\
The template syntax is small, and its limits are the part worth knowing.

* **Dotted paths only.** `{{ vars.node1.out }}`, `{{ vars.node1.out.items.0 }}`
  and `{{ vars.node1.out.items.0.title }}` all work. A list index must be a
  literal digit — there is **no computed index**, so
  `{{ vars.plan.out.queries[vars.gate.iteration] }}` is not a template, it is a
  literal string that renders empty. To pick an item by a runtime counter, put a
  Code node in front of this one and let the snippet do the indexing.
* **Outside `vars`, the names are the state channels.** `{{ inputs.in }}` is what
  arrived on this node's own input handle; `{{ params.<key> }}` is this node's
  config; a bare `{{ some_field }}` reads a declared shared-state channel.
* **A dict or list renders as JSON text**, which is usually what you want when
  feeding a model a structure.
* `missing: empty` renders an unresolved placeholder as nothing, which turns a
  typo into a quietly incomplete sentence. `missing: error` fails the node and
  names the placeholder. Prefer `error` while building a graph and `empty` once
  it is known good — especially in a graph a user did not write, where a silent
  empty is the failure mode nobody notices.
* A list arriving on the input handle is joined with `separator`.\
""",
)


@register_node
class PromptNode(BaseNode):
    spec = PROMPT_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        config = self.resolve_config(ctx)

        scope = template_scope(ctx, config)

        template = config.get("template") or ""
        if not template.strip():
            raise ValueError("Prompt Template node has an empty template.")

        missing: list[str] = []
        text = render(template, scope, on_missing=missing.append)

        if missing:
            unique = sorted(set(missing))
            if (config.get("missing") or "empty") == "error":
                raise ValueError(
                    "Template referenced values that are not available: "
                    + ", ".join(unique)
                )
            await ctx.log(
                f"Rendered {len(unique)} unresolved placeholder(s) as empty: "
                + ", ".join(unique),
                "warning",
            )

        # Flatten a list input into readable text.
        separator = config.get("separator") or "\n\n"
        if isinstance(text, list):
            text = separator.join(str(item) for item in text)

        await ctx.log(f"Rendered {len(text)} characters.", "info")
        return {
            "out": text,
            "vars": {
                "template": template,
                "characters": len(text),
                "missing": sorted(set(missing)),
            },
        }


__all__ = ["PROMPT_SPEC", "PromptNode"]
