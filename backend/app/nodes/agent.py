"""Agent node — a reason/act loop with tools.

Implements the classic ReAct cycle explicitly rather than delegating to a
framework factory:

    think -> call tools -> observe -> think -> ... -> answer

Two reasons for doing it by hand:
  * every intermediate step is emitted to the run timeline, so you can watch the
    agent work instead of guessing why it answered the way it did;
  * it has no dependency on the version-specific shape of ``create_agent``.
"""

from __future__ import annotations

import json
from typing import Any

from app.runtime.ai.gateway import ModelUsage, get_model_gateway
from app.graph.schema import (
    BooleanParam,
    InputPort,
    JsonParam,
    ModelParam,
    NodeCategory,
    NodeTypeSpec,
    NumberParam,
    OutputPort,
    PortType,
    StringParam,
)
from app.nodes.base import BaseNode, NodeContext, context_text, template_scope
from app.nodes.registry import register_node
from app.runtime.template import find_placeholders, render
from app.tools.registry import ToolError, coerce_arguments, get_tool_registry

AGENT_SPEC = NodeTypeSpec(
    type="agent",
    label="Agent",
    label_zh="智能体",
    category=NodeCategory.MODEL,
    description="Reason in a loop and decide which tools to call until it can answer.",
    description_zh="循环推理，自行决定该调用哪些工具，直到能够给出回答。",
    icon="bot",
    color="#8b5cf6",
    supports_streaming=True,
    inputs=[
        InputPort(
            id="in",
            label="Context",
            label_zh="上下文",
            type=PortType.ANY,
            description="Supplies template variables and/or upstream output.",
            description_zh="提供模板变量和/或上游节点的输出。",
        )
    ],
    outputs=[
        OutputPort(id="out", label="Answer", label_zh="回答", type=PortType.TEXT),
        OutputPort(id="messages", label="Messages", label_zh="消息列表", type=PortType.MESSAGES),
        OutputPort(id="trace", label="Steps", label_zh="步骤", type=PortType.JSON),
    ],
    params=[
        StringParam(
            name="system_prompt",
            label="System prompt",
            label_zh="系统提示词",
            description="The agent's role and rules. Tell it when to use tools.",
            description_zh="智能体的角色与规则，并说明何时应该使用工具。",
            multiline=True,
            rows=4,
            default=(
                "You are a capable assistant. Use the available tools when they help "
                "you answer accurately; otherwise answer directly. "
                "When you have enough information, stop calling tools and reply."
            ),
        ),
        StringParam(
            name="task",
            label="Task",
            label_zh="任务",
            description=(
                "What the agent should accomplish. Supports {{ }} placeholders. "
                "Leave blank to hand the agent whatever arrived on the context port."
            ),
            description_zh=(
                "希望智能体完成什么。支持 {{ }} 占位符。"
                "留空即可——智能体会直接收到「上下文」端口传来的内容。"
            ),
            multiline=True,
            rows=5,
            default="",
        ),
        JsonParam(
            name="tools",
            label="Available tools",
            label_zh="可用工具",
            description=(
                "List of tool names. Leave empty to allow every registered tool. "
                "Run the backend and open /api/tools to see what is available."
            ),
            description_zh=(
                "工具名列表。留空则允许使用所有已注册的工具。"
                "启动后端后打开 /api/tools 可以查看有哪些工具。"
            ),
            rows=5,
            default=[],
        ),
        ModelParam(name="model", label="Model", label_zh="模型"),
        NumberParam(
            name="max_iterations",
            label="Max reasoning steps",
            label_zh="最大推理步数",
            description="Stops a runaway loop. Each step may call several tools.",
            description_zh="防止无限循环。每一步可能会调用多个工具。",
            default=5,
            min=1,
            max=25,
            step=1,
            integer=True,
        ),
        NumberParam(
            name="temperature",
            label="Temperature",
            label_zh="温度",
            default=0.0,
            min=0,
            max=2,
            step=0.1,
            advanced=True,
        ),
        BooleanParam(
            name="fail_on_tool_error",
            label="Fail on tool error",
            label_zh="工具报错时失败",
            description="When off, tool errors are fed back to the model to recover from.",
            description_zh="关闭时，工具报错会回传给模型，让它自行调整。",
            default=False,
            advanced=True,
        ),
    ],
    ai_notes="""\
A real reason/act loop: think → call tools → observe → repeat → answer. Every
step is emitted to the run timeline, which is why this node is worth using
instead of a single LLM call when the task needs looking things up.

* **`tools` is a list of registered tool names.** Empty means "every registered
  tool", which is rarely what you want — an agent with the database tools, the
  network tools and the calculator all bound wanders. Name the two or three it
  actually needs. The names available in this deployment are listed in the
  `add_node` tool's description; a name that is not registered fails the node.
* **`max_iterations` is the reasoning budget, not a retry count.** Each step may
  call several tools. A search-and-verify agent needs 3-5. Setting it to 1 or 2
  produces an agent that answers from memory while looking like it researched.
* **The budget is a hard stop, not an error.** When it runs out the node returns
  whatever the model last said — which may be a half-finished answer with no
  JSON in it. Downstream parsing must survive that.
* **`out` is free text.** If a later node parses JSON out of it, say so
  explicitly in `system_prompt` (give the exact shape) and still expect the model
  to wrap it in a ``` fence or add a sentence of preamble. Strip fences before
  parsing, and have a fallback path when parsing yields nothing.
* **`task` and `system_prompt` both take `{{ }}` templates**, so upstream data
  reaches the agent without a separate Prompt node. Keep the injected material
  small — a 10,000-character page of scraped HTML dilutes the instruction.
* `fail_on_tool_error: false` is usually right for a research agent: a blocked
  URL becomes something the model routes around instead of a failed run.\
""",
)


@register_node
class AgentNode(BaseNode):
    spec = AGENT_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )

        config = self.resolve_config(ctx)

        scope = template_scope(ctx, config)

        missing: list[str] = []
        system_text = render(
            config.get("system_prompt") or "", scope, on_missing=missing.append
        )

        task_template = config.get("task") or ""
        placeholders = find_placeholders(task_template)
        task = render(task_template, scope, on_missing=missing.append)
        unresolved = sorted(set(placeholders) & set(missing))

        # Same rule as the LLM node: the context port is the data path, and the
        # template is only how that data gets reshaped. A task that named a
        # variable which does not exist used to be handed to the model with the
        # name simply missing from the sentence — worse than empty, because it
        # reads like a complete instruction.
        if unresolved:
            await ctx.log(
                f"{', '.join(unresolved)} did not resolve; sent the context value instead.",
                "warning",
            )
        if not placeholders or unresolved:
            arrived = context_text(ctx)
            if arrived:
                task = f"{task}\n\n{arrived}" if task.strip() else arrived

        # Still empty means there is genuinely nothing to do: no task written and
        # nothing connected. That is a real failure and says so, rather than
        # sending the model an agent with no instructions.
        if not task.strip():
            raise ValueError("Agent node has an empty task after templating.")

        # ---- tool selection ------------------------------------------------
        try:
            tools = _select_tools(config.get("tools"))
        except ToolError as exc:
            raise ValueError(str(exc)) from exc

        registry = get_tool_registry()
        # Through the capability layer, not the provider factory. The Agent
        # node is the reason usage has to be cumulative: one run is several
        # model calls, one per reasoning step, so "what did this cost" is a sum
        # and there is no single response to read it from.
        gateway = get_model_gateway()
        model_overrides = config.get("model") or {}
        schemas = registry.schemas([tool.name for tool in tools])
        total_usage = ModelUsage()

        messages: list[Any] = []
        if system_text.strip():
            messages.append(SystemMessage(content=system_text.strip()))
        messages.append(HumanMessage(content=task))

        await ctx.log(
            f"Agent started with {len(tools)} tool(s) and a "
            f"{int(config.get('max_iterations') or 5)}-step budget.",
            "info",
        )

        max_iterations = int(config.get("max_iterations") or 5)
        trace: list[dict[str, Any]] = []
        answer = ""

        for step in range(1, max_iterations + 1):
            reply = await gateway.complete(
                messages, overrides=model_overrides, tools=schemas or None
            )
            total_usage.merge(reply.usage)
            response = reply.message
            messages.append(response)

            tool_calls = getattr(response, "tool_calls", None) or []
            text = _text_of(response)
            if text and not tool_calls:
                # A reply with no tool calls is the final answer.
                answer = text

            trace.append(
                {
                    "step": step,
                    "thought": text,
                    "tool_calls": [
                        {"name": call.get("name"), "arguments": call.get("args")}
                        for call in tool_calls
                    ],
                }
            )

            if not tool_calls:
                await ctx.log(f"Agent finished after {step} step(s).", "info")
                break

            await ctx.emit(
                {"agent_step": step, "tool_calls": [c.get("name") for c in tool_calls]}
            )

            for call in tool_calls:
                name = str(call.get("name") or "")
                call_id = str(call.get("id") or f"call_{step}_{name}")
                raw_args = call.get("args") or {}

                try:
                    tool = registry.get(name)
                    arguments = coerce_arguments(raw_args)
                    await ctx.log(f"Step {step}: calling {name}({_short(arguments)})")
                    observation = await tool(**arguments)
                    rendered = _to_text(observation)
                    trace[-1]["tool_results"] = trace[-1].get("tool_results", [])
                    trace[-1]["tool_results"].append(
                        {"name": name, "ok": True, "result": rendered[:2000]}
                    )
                except Exception as exc:  # noqa: BLE001 - reported back to the model
                    rendered = f"Error: {type(exc).__name__}: {exc}"
                    await ctx.log(f"Step {step}: {name} failed - {exc}", "error")
                    trace[-1]["tool_results"] = trace[-1].get("tool_results", [])
                    trace[-1]["tool_results"].append(
                        {"name": name, "ok": False, "result": rendered}
                    )
                    if config.get("fail_on_tool_error"):
                        raise ValueError(
                            f"Agent tool '{name}' failed: {exc}"
                        ) from exc

                messages.append(ToolMessage(content=rendered, tool_call_id=call_id))
        else:
            await ctx.log(
                f"Agent hit its {max_iterations}-step budget and returned its last answer.",
                "warning",
            )

        if not answer:
            answer = _text_of(messages[-1]) or "(the agent produced no answer)"

        if not total_usage.is_empty:
            await ctx.log(
                f"Agent used {total_usage.total_tokens} token(s) over "
                f"{total_usage.calls} call(s) "
                f"({total_usage.prompt_tokens} in / {total_usage.completion_tokens} out).",
                "info",
            )
            # On the trace as well as in the log: the per-step record is what a
            # cost breakdown would be built from, and the log is not queryable.
            #
            # The emit arrives as a `node_output_chunk` event — `ctx.emit` is
            # hard-wired to that kind, so check `data.usage` rather than a
              # distinct event type. See `docs/log/phase-1-3-gateway.md#g8`.
            trace.append({"usage": total_usage.as_dict()})
            await ctx.emit({"usage": total_usage.as_dict()})

        return {
            "out": answer,
            "messages": messages,
            "trace": trace,
        }


def _select_tools(requested: Any) -> list[Any]:
    """Resolve the node's ``tools`` config against the active registry.

    Extracted from ``run`` so the scoping rule is testable without driving a
    model: the interesting behaviour here is *which* tools an agent is allowed,
    and that question has nothing to do with inference.

    An empty or absent list means "every tool this agent is allowed". That is
    deliberately not the same as "every tool that happens to be registered on
    this host" — see :mod:`app.tools.registry`.

    Reading the registry explicitly is for clarity rather than safety: the
    module-level ``list_tools``/``get_tool`` delegate to it, so both spellings
    behave the same. The behaviour the tests pin is that this function cannot
    reach past the configured registry into the built-in store.
    """
    registry = get_tool_registry()
    names = [str(name) for name in requested] if isinstance(requested, list) else []
    if not names:
        return registry.list()
    return [registry.get(name) for name in names]


def _text_of(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts).strip()
    return ""


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _short(value: Any, limit: int = 120) -> str:
    text = _to_text(value)
    return text if len(text) <= limit else text[:limit] + "..."


__all__ = ["AGENT_SPEC", "AgentNode"]
