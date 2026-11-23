"""LLM node — the workhorse.

Renders a prompt template against graph state, calls the chat model, and streams
tokens back through the run event bus so the UI can show output as it arrives.

Streaming is done here (rather than relying on LangGraph's ``messages`` stream
mode) because the node itself knows its own id, which makes per-node attribution
in the UI exact and independent of engine plumbing.
"""

from __future__ import annotations

import json
from typing import Any

from app.runtime.ai.gateway import ModelUsage, UsageLimitExceeded, get_model_gateway
from app.graph.schema import (
    BooleanParam,
    InputPort,
    ModelParam,
    NodeCategory,
    NodeTypeSpec,
    NumberParam,
    OutputPort,
    PortType,
    SelectOption,
    SelectParam,
    StringParam,
)
from app.nodes.base import BaseNode, NodeContext, context_text, template_scope
from app.nodes.registry import register_node
from app.runtime.template import find_placeholders, render

LLM_SPEC = NodeTypeSpec(
    type="llm",
    label="LLM",
    label_zh="大模型",
    category=NodeCategory.MODEL,
    description="Call a chat model with a prompt built from the graph's state.",
    description_zh="用提示词调用对话模型，提示词可以引用上游节点的输出。",
    icon="sparkles",
    color="#a855f7",
    supports_streaming=True,
    inputs=[
        InputPort(
            id="in",
            label="Context",
            label_zh="上下文",
            type=PortType.ANY,
            description="Optional. Supplies template variables and/or upstream output.",
            description_zh="可选。用于提供模板变量和/或上游节点的输出。",
        ),
    ],
    outputs=[
        OutputPort(id="out", label="Text", label_zh="文本", type=PortType.TEXT),
        OutputPort(id="message", label="Message", label_zh="消息", type=PortType.MESSAGE),
    ],
    params=[
        StringParam(
            name="system_prompt",
            label="System prompt",
            label_zh="系统提示词",
            description="Sets the model's role and constraints.",
            description_zh="设定模型的角色和约束。",
            multiline=True,
            rows=3,
            default="You are a helpful assistant.",
        ),
        StringParam(
            name="prompt",
            label="Prompt",
            label_zh="提示词",
            description=(
                "The user message. Reference earlier values with {{ }} placeholders, "
                "e.g. {{ query }} or {{ vars.node_1.out }}. Leave blank to hand the "
                "model whatever arrived on the context port."
            ),
            description_zh=(
                "用户消息。用 {{ }} 占位符引用之前的值，"
                "例如 {{ query }} 或 {{ vars.node_1.out }}。"
                "留空即可——模型会直接收到「上下文」端口传来的内容。"
            ),
            multiline=True,
            rows=6,
            default="",
        ),
        ModelParam(
            name="model",
            label="Model",
            label_zh="模型",
            description="Leave blank to use the global model configured in Settings.",
            description_zh="留空则使用「设置」里配置的全局模型。",
        ),
        SelectParam(
            name="output_format",
            label="Output format",
            label_zh="输出格式",
            default="text",
            options=[
                SelectOption(label="Text", label_zh="文本", value="text"),
                SelectOption(
                    label="JSON",
                    value="json",
                    description="Ask the provider for a JSON object. Falls back to parsing.",
                    description_zh="请求服务端返回 JSON 对象；失败时回退到文本解析。",
                ),
            ],
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
        NumberParam(
            name="max_tokens",
            label="Max tokens",
            label_zh="最大 token 数",
            default=1024,
            min=1,
            step=64,
            integer=True,
            advanced=True,
        ),
        BooleanParam(
            name="include_history",
            label="Include conversation history",
            label_zh="包含对话历史",
            description="Prepend the graph's `messages` channel, if any.",
            description_zh="把图里 `messages` 通道的内容放到最前面（如果有）。",
            default=False,
        ),
    ],
    ai_notes="""\
One call, one answer. No tools, no loop, no ability to look anything up — if the
task needs a fact the model does not have, this is the wrong node and an Agent
node is the right one.

* `prompt` takes `{{ }}` templates, so upstream values can be injected directly.
  A Prompt node in front of this one is only worth it when the template is long
  enough to want editing on its own.
* `output_format: json` asks the provider for a JSON response. It improves
  compliance but is not a guarantee — a downstream parser should still strip
  code fences and handle a failed parse.
* `out` is the text; `message` is the message object for anything that consumes
  a conversation.
* `max_tokens` defaulting to 1024 truncates long deliverables mid-sentence. A
  report generator wants 2000 or more.
* `temperature: 0` for extraction and classification, higher only for prose.\
""",
)


@register_node
class LLMNode(BaseNode):
    spec = LLM_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        from langchain_core.messages import (
            AIMessage,
            BaseMessage,
            HumanMessage,
            SystemMessage,
        )

        config = self.resolve_config(ctx)

        # ---- 1. Build the template scope ---------------------------------
        scope = template_scope(ctx, config)

        missing: list[str] = []
        system_text = render(
            config.get("system_prompt") or "", scope, on_missing=missing.append
        )

        prompt_template = config.get("prompt") or ""
        placeholders = find_placeholders(prompt_template)
        user_text = render(prompt_template, scope, on_missing=missing.append)
        unresolved = sorted(set(placeholders) & set(missing))

        # The context port is this node's data path; the prompt is only how that
        # data gets *reshaped* on its way to the model. Those two used to be
        # conflated: a fresh node shipped with `prompt = {{ query }}`, there is
        # usually no variable by that name, the template rendered to nothing, and
        # the model — handed an empty message — answered honestly that it could
        # not see the question. A green run, no error, and a non-answer as the
        # result, which is the worst outcome this node has.
        #
        # So whenever the template did not actually pull anything in, what
        # arrived is sent instead. A connected node always has something to work
        # with; a template changes the shape of the data, it is not the only way
        # the data can travel.
        if unresolved:
            await ctx.log(
                f"{', '.join(unresolved)} did not resolve; sent the context value instead.",
                "warning",
            )
        if not placeholders or unresolved:
            arrived = context_text(ctx)
            if arrived:
                user_text = f"{user_text}\n\n{arrived}" if user_text.strip() else arrived

        # ---- 2. Assemble messages ----------------------------------------
        messages: list[Any] = []
        if system_text.strip():
            messages.append(SystemMessage(content=system_text.strip()))
        if config.get("include_history"):
            # The `messages` channel may hold real message objects or plain
            # dicts (e.g. seeded from a run input). Normalise to objects.
            for item in ctx.messages:
                if isinstance(item, BaseMessage):
                    messages.append(item)
                elif isinstance(item, dict) and "role" in item:
                    messages.append(_dict_to_message(item, HumanMessage, AIMessage, SystemMessage))
        messages.append(HumanMessage(content=user_text))

        # ---- 3. Call the model, streaming if we can ----------------------
        #
        # Through the capability layer. Two things this buys beyond tidiness:
        # the provider quirk for JSON output stops living in a node, and the
        # token counts come back attached to the answer instead of being
        # unavailable (see `app/runtime/ai/gateway.py`).
        gateway = get_model_gateway()
        overrides = config.get("model") or {}
        json_mode = (config.get("output_format") or "text") == "json"

        usage = ModelUsage()
        text = ""
        try:
            async for chunk in gateway.stream(
                messages, overrides=overrides, json_mode=json_mode
            ):
                if chunk.usage is not None:
                    usage.merge(chunk.usage)
                if not chunk.text:
                    continue
                text += chunk.text
                await ctx.emit({"chunk": chunk.text, "text": text})
        except UsageLimitExceeded:
            # A quota stop is not a streaming failure. Retrying without stream
            # would spend another call on an already-exhausted budget.
            raise
        except Exception as exc:  # noqa: BLE001
            await ctx.log(f"Streaming failed ({exc}); retrying without stream.", "warning")
            reply = await gateway.complete(
                messages, overrides=overrides, json_mode=json_mode
            )
            text = reply.text
            usage = reply.usage

        text = (text or "").strip()
        if not text:
            raise ValueError(
                f"Model '{config.get('model') or 'default'}' returned an empty response."
            )

        if not usage.is_empty:
            await ctx.log(
                f"Tokens: {usage.prompt_tokens} in / {usage.completion_tokens} out "
                f"= {usage.total_tokens} over {usage.calls} call(s).",
                "info",
            )
            # Emitted as well as logged: a cost panel needs this as data, and a
            # log line is not queryable.
            #
            # Note the event arrives as `node_output_chunk` — `ctx.emit` is
            # hard-wired to that kind (see `graph/compiler.py`), so a payload
            # with no `text` shares a type with the streamed tokens. The frontend
            # guards on `typeof text === 'string'` and ignores it safely; a
            # consumer of the usage figure has to look for `data.usage` on a
            # chunk event rather than for a distinct type. Verified rather than
              # assumed — see `docs/log/phase-1-3-gateway.md#g8`.
            await ctx.emit({"usage": usage.as_dict()})

        parsed: Any = None
        if (config.get("output_format") or "text") == "json":
            parsed = _parse_json(text)
            if parsed is None:
                await ctx.log("Response was not valid JSON; returning raw text.", "warning")

        await ctx.log(f"Model returned {len(text)} characters.", "info")

        final_value = parsed if parsed is not None else text
        return {
            "out": final_value,
            "message": AIMessage(content=text),
        }


def _dict_to_message(
    item: dict[str, Any],
    human_cls: type,
    ai_cls: type,
    system_cls: type,
) -> Any:
    """Convert a ``{"role": ..., "content": ...}`` dict into a message object."""
    role = str(item.get("role", "user")).lower()
    content = item.get("content", "")
    if role in ("system", "developer"):
        return system_cls(content=content)
    if role in ("assistant", "ai"):
        return ai_cls(content=content)
    return human_cls(content=content)


def _parse_json(text: str) -> Any | None:
    """Tolerantly parse JSON, unwrapping ```json fences if present."""
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None


__all__ = ["LLM_SPEC", "LLMNode"]
