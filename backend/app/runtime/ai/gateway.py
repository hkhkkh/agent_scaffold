"""Model-calling capability — the seam between a node and the model provider.

Why this module exists
----------------------

:mod:`app.runtime.ai.provider` already answers *which* model and *which credentials*.
It does not answer the question Phase 5 needs: **how much did that cost?**
Token counts come back attached to the *response*, so the count has to be read
where the response is handled — and that is the call site, not the factory.
There were five call sites, each calling ``model.ainvoke``/``model.astream`` in
its own way, and no shared point where a usage number could be collected.

So this interface exposes the **call**, not the constructor::

    gateway = get_model_gateway()
    reply = await gateway.complete(messages, overrides=..., json_mode=True)
    reply.usage.total_tokens          # <- the thing Phase 5 needs

If it exposed only ``build_model(...)`` returning a model, cost tracking would
still have to be added at every call site — which is exactly the situation this
removes.

Two smaller things fall out of the same move:

* **Provider quirks stop leaking into nodes.** ``llm.py`` currently knows that a
  JSON response is requested with ``response_format={"type": "json_object"}``
  and that it must catch a failure. That is knowledge about OpenAI-compatible
  APIs, not about prompting, so it moves here.
* **Streaming usage becomes possible at all.** OpenAI-compatible endpoints omit
  token counts from streamed responses unless the request asks for them
  (``stream_usage``). Nobody was asking, so a streamed answer was
  uncountable. One flag, one place.

The shape follows :mod:`app.runtime.egress`, :mod:`app.runtime.runner` and
:mod:`app.tools.registry` — ABC, a default implementation, a process-wide
injection point, and a builder that reads configuration. Four modules with the
same shape is a convention; a fifth invented shape would be a mistake.

Why the AI helpers are not migrated
------------------------------------

``gen_graph``, ``gen_node`` and ``repair`` still build models directly. They are
**platform** features — a person clicking "AI fill" — not agents built on the
scaffold. Their spend belongs to the platform's budget, not to any agent's, so
counting them in the same place would mix two things an operator wants separate.
Moving them later is straightforward if that judgement turns out wrong.

Nodes are different: an agent's model calls are the agent's cost, and the
architecture requirement is that nodes depend on interfaces rather than on
concrete providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Sequence

from app.runtime.ai.provider import build_chat_model
from app.settings import Settings, get_settings

__all__ = [
    "LangChainModelGateway",
    "ModelChunk",
    "ModelGateway",
    "ModelReply",
    "ModelUsage",
    "UsageLimitExceeded",
    "build_model_gateway",
    "get_model_gateway",
    "message_text",
    "set_model_gateway",
    "set_usage_sink",
    "usage_from_message",
]


class UsageLimitExceeded(Exception):
    """A usage sink refused the call because the caller's budget is exhausted.

    Runtime code must not retry this. The platform's quota exception inherits
    from it, while this module stays free of any platform/security import.
    """


#: Optional observer for model spend. The web application registers the quota
#: ledger here; an exported agent leaves it unset. Keeping this as a callback
#: (rather than importing the platform's security package) is what keeps the
#: runtime importable without FastAPI or the platform database.
_usage_sink: Callable[[Any], None] | None = None


def set_usage_sink(sink: Callable[[Any], None] | None) -> None:
    global _usage_sink
    _usage_sink = sink


def _notify_usage(usage: Any) -> None:
    sink = _usage_sink
    if sink is not None:
        sink(usage)


@dataclass
class ModelUsage:
    """Token counts, summed across however many calls produced them.

    A running total rather than a snapshot because the caller almost always
    wants "what did this node cost", which for an Agent node is several calls
    (one per reasoning step).
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    @property
    def is_empty(self) -> bool:
        """True when the provider reported nothing — see :func:`usage_from_message`.

        Worth distinguishing from zero: a local endpoint that does not report
        usage is not the same as a call that used no tokens.
        """
        return self.total_tokens == 0 and self.prompt_tokens == 0

    def merge(self, other: ModelUsage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.calls += other.calls

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }


@dataclass
class ModelChunk:
    """One piece of a streamed answer.

    ``usage`` is set on the **final** chunk only, because that is when the
    provider reports it. A caller that wants the total must therefore keep
    reading to the end — which it does anyway, to finish the answer.
    """

    text: str = ""
    usage: ModelUsage | None = None


@dataclass
class ModelReply:
    """A complete (non-streamed) answer plus what it cost."""

    text: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    model: str = ""
    message: Any = None
    """The provider's own message object, for callers that need tool calls."""


def usage_from_message(message: Any) -> ModelUsage | None:
    """Read token counts off a LangChain message, or ``None`` if it has none.

    Returns ``None`` rather than a zeroed :class:`ModelUsage` on purpose. Most
    chunks of a stream carry no counts, so a zero would be indistinguishable
    from "this chunk cost nothing" and would inflate the call count if merged.
    The ``None`` propagates and the caller decides whether absence is a problem.
    """
    metadata = getattr(message, "usage_metadata", None)
    if not isinstance(metadata, dict) or not metadata:
        return None

    def pick(*keys: str) -> int:
        for key in keys:
            value = metadata.get(key)
            if isinstance(value, int):
                return value
        return 0

    prompt = pick("input_tokens", "prompt_tokens")
    completion = pick("output_tokens", "completion_tokens")
    total = pick("total_tokens") or (prompt + completion)
    if not (prompt or completion or total):
        return None
    return ModelUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        calls=1,
    )


def message_text(message: Any) -> str:
    """Flatten a message's content to text.

    Handles the list-of-parts shape that newer providers return for multimodal
    output. Lives here rather than in a node because the shape is the provider's,
    not the node's.
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


class ModelGateway:
    """Calls a model and reports what it cost.

    Implementations must:

    * return the text a caller can use **and** the usage alongside it — a
      gateway that drops usage makes this interface pointless;
    * raise on failure rather than returning an empty string, so a caller can
      tell "the model said nothing" from "the model was not reached";
    * treat ``overrides`` the way :func:`app.runtime.ai.provider.resolve_llm_config`
      does, credentials included — this interface must not become a way around
      the rule that a credential does not follow a redirect.
    """

    async def complete(
        self,
        messages: Sequence[Any],
        *,
        overrides: dict[str, Any] | None = None,
        json_mode: bool = False,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelReply:
        raise NotImplementedError

    def stream(
        self,
        messages: Sequence[Any],
        *,
        overrides: dict[str, Any] | None = None,
        json_mode: bool = False,
    ) -> AsyncIterator[ModelChunk]:
        raise NotImplementedError


@dataclass
class LangChainModelGateway(ModelGateway):
    """Default implementation: whatever :mod:`app.runtime.ai.provider` builds.

    Deliberately thin. The point is not to replace the provider layer but to put
    one object between a node and it, so the call — and therefore the cost — has
    somewhere to be observed.

    ``stream_usage`` is offered because a streamed response is otherwise
    uncountable: OpenAI-compatible APIs omit the counts unless asked. A provider
    that rejects the flag is not a problem — ``build_chat_model`` falls back to
    a model without it, and the stream simply reports no usage.

    ``tools`` is exposed for the Agent node. Binding the schemas is the
    provider's job rather than the node's, and putting it here keeps the Agent
    node free of any knowledge about how a tool schema reaches the model.
    """

    settings: Settings | None = None

    def _model(
        self,
        overrides: dict[str, Any] | None,
        *,
        streaming: bool,
        json_mode: bool,
        stream_usage: bool,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        model = build_chat_model(
            overrides,
            settings=self.settings,
            streaming=streaming,
            stream_usage=stream_usage,
            tools=tools,
        )
        if json_mode:
            try:
                return model.bind(response_format={"type": "json_object"})
            except Exception:  # noqa: BLE001 - provider may not support it
                # Not an error: the prompt is expected to describe the shape
                # anyway. The caller logs it, because "the provider ignored your
                # JSON request" changes how much to trust the output.
                return model
        return model

    async def complete(
        self,
        messages: Sequence[Any],
        *,
        overrides: dict[str, Any] | None = None,
        json_mode: bool = False,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelReply:
        model = self._model(
            overrides,
            streaming=False,
            json_mode=json_mode,
            stream_usage=True,
            tools=tools,
        )
        response = await model.ainvoke(list(messages))
        usage = usage_from_message(response) or ModelUsage(calls=1)
        # Public mode: charge the visitor/IP/global budgets before returning.
        # Outside public mode the sink is unset or the context lookup is a no-op.
        _notify_usage(usage)
        return ModelReply(
            text=message_text(response).strip(),
            usage=usage,
            model=str((overrides or {}).get("model") or ""),
            message=response,
        )

    async def stream(
        self,
        messages: Sequence[Any],
        *,
        overrides: dict[str, Any] | None = None,
        json_mode: bool = False,
    ) -> AsyncIterator[ModelChunk]:
        model = self._model(
            overrides, streaming=True, json_mode=json_mode, stream_usage=True
        )
        usage = ModelUsage()
        saw_usage = False

        async for chunk in model.astream(list(messages)):
            delta = usage_from_message(chunk)
            if delta is not None:
                usage.merge(delta)
                saw_usage = True
            text = message_text(chunk)
            if text:
                yield ModelChunk(text=text)

        # One call happened whether or not the provider said so. Reporting
        # `calls=0` for a call that clearly ran would make the total misleading
        # in the other direction.
        if not saw_usage:
            usage = ModelUsage(calls=1)
        _notify_usage(usage)
        yield ModelChunk(usage=usage)


def build_model_gateway(settings: Settings | None = None) -> ModelGateway:
    """Construct the gateway from configuration.

    No branching yet — there is one implementation. The function exists so that
    the injection point has a configuration-shaped entry, matching the other
    three capability modules; a future per-tenant or proxied gateway plugs in
    here without any caller changing.
    """
    return LangChainModelGateway(settings=settings or get_settings())


#: Process-wide gateway. Swapped in tests, and the seam where a budget-enforcing
#: or audit-recording wrapper plugs in without touching a node.
_gateway: ModelGateway | None = None


def get_model_gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = build_model_gateway()
    return _gateway


def set_model_gateway(gateway: ModelGateway | None) -> None:
    """Override the process gateway. Passing ``None`` restores the configured one."""
    global _gateway
    _gateway = gateway
