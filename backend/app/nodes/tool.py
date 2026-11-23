"""Tool node — deterministic tool execution.

The distinction from the Agent node matters and is worth preserving:
this node calls **exactly the tool you configured, with exactly the arguments
you configured**. No model decides anything. That makes it cheap, repeatable,
and safe to put in a loop.

Use the Agent node when the *choice* of tool should be a model decision.
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
    SelectOption,
    SelectParam,
)
from app.nodes.base import BaseNode, NodeContext, template_scope
from app.nodes.registry import register_node, register_spec_refresher
from app.runtime.template import render
from app.tools.registry import (
    ToolError,
    coerce_arguments,
    get_tool,
    get_tool_registry,
    validate_arguments,
)


def tool_options() -> list[SelectOption]:
    """The dropdown's contents, read from the live registry.

    Deliberately a function and not a module constant. The registry is a
    capability — :mod:`app.tools.registry` explains why — so an agent may be
    deployed with a bounded tool set. Options frozen at import would offer tools
    that do not exist here, and the user would only find out when the run failed.
    """
    return [
        SelectOption(
            label=tool.label,
            label_zh=tool.label_zh,
            value=tool.name,
            description=tool.description,
            description_zh=tool.description_zh,
        )
        for tool in get_tool_registry().list()
    ]


def build_tool_spec() -> NodeTypeSpec:
    """Build the spec from the current registry. Also the refresher below."""
    options = tool_options()
    return NodeTypeSpec(
        type="tool",
        label="Tool",
        label_zh="工具",
        category=NodeCategory.INTEGRATION,
        description="Call one specific tool with arguments you control. No model involved.",
        description_zh="用你指定的参数调用某一个工具，不经过模型。",
        icon="wrench",
        color="#14b8a6",
        inputs=[
            InputPort(
                id="in",
                label="Context",
                label_zh="上下文",
                type=PortType.ANY,
                description="Supplies template variables for the arguments below.",
                description_zh="为下面的参数提供模板变量。",
            )
        ],
        outputs=[
            OutputPort(id="out", label="Result", label_zh="结果", type=PortType.JSON),
            OutputPort(id="text", label="Text", label_zh="文本", type=PortType.TEXT),
        ],
        params=[
            SelectParam(
                name="tool",
                label="Tool",
                label_zh="工具",
                description="The capability to invoke.",
                description_zh="要调用的能力。",
                required=True,
                default=options[0].value if options else "",
                options=options,
            ),
            JsonParam(
                name="arguments",
                label="Arguments",
                label_zh="参数",
                description=(
                    "JSON object. String values may contain {{ }} placeholders, "
                    "e.g. {\"url\": \"{{ vars.start.out.url }}\"}."
                ),
                description_zh=(
                    "JSON 对象。字符串值里可以用 {{ }} 占位符，"
                    "例如 {\"url\": \"{{ vars.start.out.url }}\"}。"
                ),
                rows=7,
                default={},
            ),
            BooleanParam(
                name="fail_on_error",
                label="Fail the run on error",
                label_zh="出错时让运行失败",
                description="When off, the error is captured and passed downstream.",
                description_zh="关闭时，错误会被捕获并继续传给下游。",
                default=True,
            ),
        ],
        ai_notes="""\
Deterministic: it calls exactly the named tool with exactly these arguments, and
no model decides anything. That is the whole point — it is cheap, repeatable, and
safe to put inside a loop. Use the Agent node when the *choice* of tool should be
a model decision; use this when you already know which call to make.

* **`arguments` is a JSON object whose string values may contain `{{ }}`.** They
  are resolved before the tool is called, so
  `{"url": "{{ vars.search.out.results.0.url }}"}` works. This is how one search
  feeds several fetches.
* **A missing template path renders empty, and an empty required argument is
  still a call.** `{"url": "{{ vars.pick.out.items.2.url }}"}` on a list with two
  entries sends `url=""` — the tool then fails with a confusing message about the
  URL rather than about the missing item. Guard the index upstream, or set
  `fail_on_error: false` and handle the error value.
* **The tool names available in this deployment are in the `add_node` tool's
  description.** A name that is not registered — or that this deployment has
  disabled — fails the node; it does not fall back.
* **Return shape:** `out` is the tool's raw return value, `text` is a readable
  rendering. A `json_query` consuming this should read `out`; a prompt should
  read `text`.
* Set `fail_on_error: false` for a call whose failure is expected and survivable
  (a fetch of a page that may 404). The error becomes
  `{"error": "..."}` on `out`, and the node's `text` carries the message.\
""",
    )


TOOL_SPEC = build_tool_spec()

# The dropdown depends on the configured registry, so the palette must be able to
# rebuild it. See `app.nodes.registry.refresh_dynamic_specs`.
register_spec_refresher("tool", build_tool_spec)


@register_node
class ToolNode(BaseNode):
    spec = TOOL_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        tool_name = ctx.config.get("tool") or ""
        if not tool_name:
            raise ValueError("Tool node has no tool selected.")

        # Through the capability layer, not the module namespace: which tools
        # exist is a deployment decision (see `app.tools.registry`).
        #
        # Honest note, because the reverse validation forced it: this is
        # *readability*, not a guard. `get_tool()` delegates to this same
        # registry, so writing `get_tool(tool_name)` here would behave
        # identically — an earlier version of `_guardprobe_capability.py`
        # asserted otherwise and found nothing. What the tests actually protect
        # is that this node cannot reach past the configured registry into the
        # built-in store, which is what a bypass would look like.
        #
        # The lookup itself happens inside the try below.
        #
        # Resolve placeholders inside the argument values before validation, so
        # a templated URL or query string behaves like a literal one.
        config = self.resolve_config(ctx)
        scope = template_scope(ctx, config)

        raw = ctx.config.get("arguments")
        raw = _resolve_placeholders(raw, scope)

        try:
            # Inside the try so an unavailable tool is governed by
            # `fail_on_error` like every other tool failure. Looking it up first
            # meant "this tool is not enabled for this agent" escaped as a raw
            # error and ignored the switch — an inconsistency nobody would guess.
            tool = get_tool_registry().get(tool_name)
            arguments = coerce_arguments(raw)
            validate_arguments(tool, arguments)
            await ctx.log(f"Calling tool '{tool.label}' with {len(arguments)} argument(s).")
            result = await tool(**arguments)
        except ToolError as exc:
            message = f"Tool '{tool_name}' failed: {exc}"
            await ctx.log(message, "error")
            if ctx.config.get("fail_on_error", True):
                raise ValueError(message) from exc
            return {"out": {"error": str(exc)}, "text": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surfaced as a run failure
            message = f"Tool '{tool_name}' raised {type(exc).__name__}: {exc}"
            await ctx.log(message, "error")
            if ctx.config.get("fail_on_error", True):
                raise ValueError(message) from exc
            return {"out": {"error": str(exc)}, "text": str(exc)}

        await ctx.log(f"Tool '{tool.label}' returned.", "info")
        return {"out": result, "text": _readable_text(result)}


def _resolve_placeholders(value: Any, scope: dict[str, Any]) -> Any:
    """Recursively substitute ``{{ }}`` in every string inside a structure."""
    if isinstance(value, str):
        return render(value, scope)
    if isinstance(value, dict):
        return {k: _resolve_placeholders(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(item, scope) for item in value]
    return value


def _readable_text(value: Any) -> str:
    """A tool's return value as text, **indented**, because a reading eye sees it.

    Named for the choice rather than reusing `_as_text`, which two other modules
    also spell but do not mean. All three differ and the differences are
    deliberate, so the names have to differ too — the indent here is what makes a
    returned object legible in the run panel and in a downstream prompt.

    `default=str` rather than a raise: a tool may return a `datetime`, and losing
    the whole payload to a `TypeError` would be a worse outcome than an
    approximate rendering.
    """
    if isinstance(value, str):
        return value
    import json

    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


__all__ = ["TOOL_SPEC", "ToolNode", "build_tool_spec", "tool_options"]
