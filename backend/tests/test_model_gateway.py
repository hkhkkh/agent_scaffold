"""Phase 1 capability layer — the model gateway.

What these tests are actually defending
---------------------------------------

Not "the model can be called" — `test_runtime.py` already covers that. What is
new is that the *call* goes through an interface, and that the token counts come
back with it. Phase 5 wants a cost number, and a cost number can only be
collected where the response is handled. Before this, five call sites each
handled their own response and none of them read the counts.

So the valuable assertions are:

* a replaced gateway is the one that actually runs (the seam is live, not
  decorative — the failure this project has hit three times);
* an Agent node **accumulates** across its reasoning steps, because "what did
  this cost" is a sum and there is no single response holding it;
* a streamed answer reports its usage even though the provider only sends it on
  the last chunk.

The last one has a trap worth naming: most chunks carry no counts, so a zero
would be indistinguishable from "nothing happened" and would inflate the call
count if merged naively. ``usage_from_message`` returns ``None`` for those, and
the tests below pin that.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Sequence

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from app.runtime.ai.gateway import (
    LangChainModelGateway,
    ModelChunk,
    ModelGateway,
    ModelReply,
    ModelUsage,
    UsageLimitExceeded,
    build_model_gateway,
    get_model_gateway,
    message_text,
    set_model_gateway,
    usage_from_message,
)
from app.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _restore_gateway():
    """The gateway is process-wide, so every test here must restore it."""
    yield
    set_model_gateway(None)
    get_settings.cache_clear()


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


class _StubMessage:
    """Anything with ``content`` and ``usage_metadata`` — a duck type, not a message.

    Used for the usage tests rather than a real ``AIMessage`` because langchain
    validates its own ``usage_metadata``: all three counts must be present. That
    rule is langchain's, not the provider contract, and providers do differ —
    some omit ``total_tokens``, some use ``prompt_tokens``. Pinning the reader
    against the *shape it accepts* rather than against one library's validation
    is both possible and more honest.
    """

    def __init__(self, content: str = "hi", usage_metadata: Any = None) -> None:
        self.content = content
        self.usage_metadata = usage_metadata


def _message_with_usage(**counts: int) -> _StubMessage:
    return _StubMessage(usage_metadata=counts or None)


# ---------------------------------------------------------------------------
# Reading usage off a response
# ---------------------------------------------------------------------------


class TestUsageFromMessage:
    def test_reads_the_langchain_key_names(self):
        usage = usage_from_message(_message_with_usage(input_tokens=7, output_tokens=3))

        assert usage is not None
        assert usage.prompt_tokens == 7
        assert usage.completion_tokens == 3
        assert usage.total_tokens == 10  # derived when the provider omits it
        assert usage.calls == 1

    def test_prefers_the_provider_total_when_it_gives_one(self):
        """Some providers count differently; theirs wins over our arithmetic."""
        usage = usage_from_message(
            _message_with_usage(input_tokens=7, output_tokens=3, total_tokens=11)
        )

        assert usage is not None
        assert usage.total_tokens == 11

    def test_accepts_the_openai_key_names(self):
        usage = usage_from_message(
            _message_with_usage(prompt_tokens=5, completion_tokens=2)
        )

        assert usage is not None
        assert usage.total_tokens == 7

    def test_returns_none_when_the_provider_reports_nothing(self):
        """None, not a zeroed usage — this is the trap in the streaming path.

        A chunk with no counts is not a call that cost nothing; it is a chunk
        that carried no information. Returning ``ModelUsage()`` here would make
        every chunk look like a call and multiply the count.
        """
        assert usage_from_message(AIMessage(content="no metadata")) is None
        assert usage_from_message(AIMessageChunk(content="x")) is None
        assert usage_from_message(_message_with_usage()) is None
        assert usage_from_message(_StubMessage(usage_metadata={})) is None

    def test_survives_something_that_is_not_a_message(self):
        """The type varies by langchain version; a crash here would be a bad trade."""
        assert usage_from_message(object()) is None
        assert usage_from_message(None) is None


class TestUsageAccumulation:
    def test_merge_sums_everything_including_the_call_count(self):
        a = ModelUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, calls=1)
        b = ModelUsage(prompt_tokens=4, completion_tokens=6, total_tokens=10, calls=1)

        a.merge(b)

        assert a.as_dict() == {
            "prompt_tokens": 14,
            "completion_tokens": 11,
            "total_tokens": 25,
            "calls": 2,
        }

    def test_is_empty_distinguishes_unreported_from_zero(self):
        """A local endpoint that reports nothing is not a free call."""
        assert ModelUsage().is_empty
        assert not ModelUsage(total_tokens=1).is_empty
        # A call that ran but reported nothing is still a call.
        assert ModelUsage(calls=1).is_empty


class TestMessageText:
    def test_plain_string_content(self):
        assert message_text(AIMessage(content="hello")) == "hello"

    def test_list_of_parts(self):
        """Newer providers return multimodal parts; the node should not care."""
        message = AIMessage(content=[{"type": "text", "text": "a"}, "b"])

        assert message_text(message) == "ab"

    def test_ignores_non_text_parts(self):
        message = AIMessage(content=[{"type": "image", "url": "x"}])

        assert message_text(message) == ""

    def test_unknown_shape_does_not_crash(self):
        assert message_text(object()) == ""


# ---------------------------------------------------------------------------
# The seam is live
# ---------------------------------------------------------------------------


class _RecordingGateway(ModelGateway):
    """Second implementation, used to prove the first is replaceable."""

    def __init__(self, *, text: str = "from the recording gateway") -> None:
        self.calls: list[dict[str, Any]] = []
        self.text = text

    async def complete(
        self,
        messages: Sequence[Any],
        *,
        overrides: dict[str, Any] | None = None,
        json_mode: bool = False,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelReply:
        self.calls.append(
            {
                "messages": list(messages),
                "overrides": overrides,
                "json_mode": json_mode,
                "tools": tools,
            }
        )
        return ModelReply(
            text=self.text,
            usage=ModelUsage(
                prompt_tokens=100, completion_tokens=20, total_tokens=120, calls=1
            ),
            message=AIMessage(content=self.text),
        )

    async def stream(
        self,
        messages: Sequence[Any],
        *,
        overrides: dict[str, Any] | None = None,
        json_mode: bool = False,
    ) -> AsyncIterator[ModelChunk]:
        self.calls.append(
            {"messages": list(messages), "overrides": overrides, "json_mode": json_mode}
        )
        for piece in ("hello", " ", "world"):
            yield ModelChunk(text=piece)
        yield ModelChunk(
            usage=ModelUsage(
                prompt_tokens=9, completion_tokens=3, total_tokens=12, calls=1
            )
        )


class TestLLMNodeGoesThroughTheGateway:
    """Not "the model works" — "the node asks the platform, not the provider"."""

    def _context(self, prompt: str = "say hi") -> tuple[Any, list[tuple[Any, ...]]]:
        from app.graph.schema import NodeSpec
        from app.nodes.base import NodeContext

        events: list[tuple[Any, ...]] = []

        async def emit(payload: dict[str, Any]) -> None:
            events.append(("emit", payload))

        async def log(text: str, level: str = "info") -> None:
            events.append(("log", text, level))

        config = {"prompt": prompt, "output_format": "text"}
        node = NodeSpec(id="n1", type="llm", label="LLM", config=config)
        return (
            NodeContext(
                node=node,
                config=config,
                inputs={"in": "x"},
                state={"vars": {}, "query": "hello"},
                settings=_settings(),
                emit=emit,
                log=log,
            ),
            events,
        )

    @staticmethod
    def _emitted(events: list[tuple[Any, ...]], key: str) -> list[dict[str, Any]]:
        """Payloads of emitted events that contain ``key``.

        The event list mixes 2-tuples (``emit``) and 3-tuples (``log``), so a
        naive comprehension unpacking two values breaks on the log entries.
        """
        return [
            payload
            for event in events
            if event[0] == "emit" and key in event[1]
            for payload in (event[1],)
        ]

    def test_a_replaced_gateway_is_the_one_that_runs(self):
        from app.nodes.llm import LLMNode

        gateway = _RecordingGateway()
        set_model_gateway(gateway)
        context, _ = self._context()

        output = asyncio.run(LLMNode(context.node).run(context))

        assert len(gateway.calls) == 1, "the node did not use the installed gateway"
        # The node streams, so the answer is the concatenated chunks — the
        # assertion that matters is that they came from the replaced gateway
        # rather than from a real model.
        assert output["out"] == "hello world"

    def test_the_gateway_receives_the_rendered_prompt(self):
        from app.nodes.llm import LLMNode

        gateway = _RecordingGateway()
        set_model_gateway(gateway)
        context, _ = self._context(prompt="query is {{ query }}")

        asyncio.run(LLMNode(context.node).run(context))

        sent = gateway.calls[0]["messages"]
        assert any("query is hello" in str(getattr(m, "content", "")) for m in sent)

    def test_streamed_usage_reaches_the_node(self):
        """The whole point: a streamed answer is countable."""
        from app.nodes.llm import LLMNode

        set_model_gateway(_RecordingGateway())
        context, events = self._context()

        asyncio.run(LLMNode(context.node).run(context))

        emitted = self._emitted(events, "usage")
        assert emitted, "usage was never emitted"
        assert emitted[0]["usage"]["total_tokens"] == 12
        assert any(
            kind == "log" and "12" in text
            for kind, text, _level in (e for e in events if e[0] == "log")
        ), "usage never reached the run timeline"

    def test_a_provider_that_reports_nothing_is_not_an_error(self):
        """Ollama and friends omit usage; that must not fail the run."""
        from app.nodes.llm import LLMNode

        class _SilentGateway(ModelGateway):
            async def complete(self, messages, **kwargs):  # type: ignore[override]
                return ModelReply(text="ok", message=AIMessage(content="ok"))

            async def stream(self, messages, **kwargs):  # type: ignore[override]
                yield ModelChunk(text="ok")
                yield ModelChunk(usage=ModelUsage(calls=1))

        set_model_gateway(_SilentGateway())
        context, events = self._context()

        output = asyncio.run(LLMNode(context.node).run(context))

        assert output["out"] == "ok"
        assert not self._emitted(events, "usage")

    def test_json_mode_is_asked_of_the_gateway_not_worked_out_in_the_node(self):
        """`response_format` is a provider quirk, so it left the node."""
        from app.nodes.llm import LLMNode

        gateway = _RecordingGateway()
        set_model_gateway(gateway)
        context, _ = self._context()
        context.config["output_format"] = "json"

        asyncio.run(LLMNode(context.node).run(context))

        assert gateway.calls[0]["json_mode"] is True

    def test_text_mode_does_not_ask_for_json(self):
        from app.nodes.llm import LLMNode

        gateway = _RecordingGateway()
        set_model_gateway(gateway)
        context, _ = self._context()

        asyncio.run(LLMNode(context.node).run(context))

        assert gateway.calls[0]["json_mode"] is False

    def test_a_usage_limit_is_not_retried_without_stream(self):
        """A quota stop is not a streaming glitch; retrying would spend more."""
        from app.nodes.llm import LLMNode

        class _LimitedGateway(ModelGateway):
            def __init__(self):
                self.completed = 0

            async def complete(self, messages, **kwargs):  # type: ignore[override]
                self.completed += 1
                return ModelReply(text="should not run", message=AIMessage(content="x"))

            async def stream(self, messages, **kwargs):  # type: ignore[override]
                raise UsageLimitExceeded()
                yield  # pragma: no cover - makes this an async generator

        gateway = _LimitedGateway()
        set_model_gateway(gateway)
        context, _ = self._context()

        with pytest.raises(UsageLimitExceeded):
            asyncio.run(LLMNode(context.node).run(context))

        assert gateway.completed == 0


class TestAgentNodeGoesThroughTheGateway:
    """The Agent node is why usage has to be cumulative."""

    def _context(self, **config_overrides: Any) -> tuple[Any, list[tuple[Any, ...]]]:
        from app.graph.schema import NodeSpec
        from app.nodes.base import NodeContext

        events: list[tuple[Any, ...]] = []

        async def emit(payload: dict[str, Any]) -> None:
            events.append(("emit", payload))

        async def log(text: str, level: str = "info") -> None:
            events.append(("log", text, level))

        config = {"task": "do the thing", "tools": [], **config_overrides}
        node = NodeSpec(id="n1", type="agent", label="Agent", config=config)
        return (
            NodeContext(
                node=node,
                config=config,
                inputs={"in": None},
                state={"vars": {}},
                settings=_settings(),
                emit=emit,
                log=log,
            ),
            events,
        )

    @staticmethod
    def _emitted(events: list[tuple[Any, ...]], key: str) -> list[dict[str, Any]]:
        return [event[1] for event in events if event[0] == "emit" and key in event[1]]

    def test_the_answer_comes_from_the_gateway(self):
        from app.nodes.agent import AgentNode

        set_model_gateway(_RecordingGateway(text="final answer"))
        context, _ = self._context()

        output = asyncio.run(AgentNode(context.node).run(context))

        assert output["out"] == "final answer"

    def test_usage_accumulates_across_reasoning_steps(self):
        """Three steps must report three hundred tokens, not one hundred.

        This is the difference between an Agent node and an LLM node, and it is
        exactly what a naive per-response read would get wrong: the last
        response would be counted and the earlier ones lost.
        """
        from app.nodes.agent import AgentNode

        class _ThreeStepGateway(ModelGateway):
            """Calls a tool twice, then answers. Each call costs 120 tokens."""

            def __init__(self) -> None:
                self.step = 0

            async def complete(self, messages, **kwargs):  # type: ignore[override]
                self.step += 1
                if self.step < 3:
                    calls = [{"name": "calculator", "args": {"expression": "1+1"}, "id": f"c{self.step}"}]
                    message = AIMessage(content="", tool_calls=calls)
                else:
                    message = AIMessage(content="done")
                return ModelReply(
                    text=message_text(message),
                    usage=ModelUsage(
                        prompt_tokens=100, completion_tokens=20, total_tokens=120, calls=1
                    ),
                    message=message,
                )

            async def stream(self, messages, **kwargs):  # type: ignore[override]
                if False:
                    yield ModelChunk()

        set_model_gateway(_ThreeStepGateway())
        context, events = self._context(tools=["calculator"])

        output = asyncio.run(AgentNode(context.node).run(context))

        usage = [payload["usage"] for payload in self._emitted(events, "usage")]
        assert usage, "agent usage was never emitted"
        assert usage[0]["total_tokens"] == 360, usage[0]
        assert usage[0]["calls"] == 3
        assert output["out"] == "done"

    def test_usage_is_on_the_trace_as_well_as_the_log(self):
        """The trace is what a cost breakdown would be built from.

        A log line is not queryable, so the number has to exist as data too.
        """
        from app.nodes.agent import AgentNode

        set_model_gateway(_RecordingGateway())
        context, _ = self._context()

        output = asyncio.run(AgentNode(context.node).run(context))

        assert output["trace"][-1].get("usage", {}).get("total_tokens") == 120

    def test_tool_schemas_are_handed_to_the_gateway(self):
        """Binding schemas is the provider's job, not the node's."""
        from app.nodes.agent import AgentNode

        gateway = _RecordingGateway()
        set_model_gateway(gateway)
        context, _ = self._context(tools=["calculator"])

        asyncio.run(AgentNode(context.node).run(context))

        tools = gateway.calls[0]["tools"]
        assert tools is not None
        assert [t["function"]["name"] for t in tools] == ["calculator"]

    def test_no_tools_means_no_binding(self):
        """An agent with no available tools must not send an empty tool list.

        Some providers treat an empty array differently from an absent one, and
        an agent that cannot call anything should look like a plain model call.
        """
        from app.nodes.agent import AgentNode
        from app.tools.registry import (
            BuiltinToolRegistry,
            ScopedToolRegistry,
            set_tool_registry,
        )

        gateway = _RecordingGateway()
        set_model_gateway(gateway)
        # A scope that permits nothing.
        #
        # Note `allow=frozenset()` would NOT do it: an empty allowlist means
        # "everything", by design (see `app.tools.registry`). Naming a tool that
        # does not exist is how a deployment ends up with an empty set.
        set_tool_registry(
            ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"nonexistent"}))
        )
        context, _ = self._context()
        try:
            asyncio.run(AgentNode(context.node).run(context))
        finally:
            set_tool_registry(None)

        assert gateway.calls[0]["tools"] is None


# ---------------------------------------------------------------------------
# Configuration and injection
# ---------------------------------------------------------------------------


class TestGatewaySelection:
    def test_the_default_is_the_langchain_gateway(self):
        assert isinstance(build_model_gateway(_settings()), LangChainModelGateway)

    def test_the_getter_is_lazy_and_the_setter_restores(self):
        assert isinstance(get_model_gateway(), ModelGateway)

        replacement = _RecordingGateway()
        set_model_gateway(replacement)
        assert get_model_gateway() is replacement

        set_model_gateway(None)
        assert get_model_gateway() is not replacement

    def test_the_default_gateway_actually_calls_the_mock_model(self):
        """The capability must not have broken the thing it abstracts.

        With no API key the provider builds `MockChatModel`, so this exercises
        the real path end to end without a network call.
        """
        gateway = build_model_gateway(_settings(llm_api_key="", llm_base_url="https://api.openai.com/v1"))

        reply = asyncio.run(
            gateway.complete([HumanMessage(content="hello there")])
        )

        assert reply.text
        assert "simulated" in reply.text.lower()
        # The mock reports no usage, so the gateway must still record the call.
        assert reply.usage.calls == 1

    def test_the_mock_model_streams_and_still_terminates(self):
        gateway = build_model_gateway(
            _settings(llm_api_key="", llm_base_url="https://api.openai.com/v1")
        )

        async def collect() -> list[ModelChunk]:
            return [chunk async for chunk in gateway.stream([HumanMessage(content="hi")])]

        chunks = asyncio.run(collect())

        assert any(chunk.text for chunk in chunks)
        assert chunks[-1].usage is not None, "the final chunk must carry the usage"


class TestStreamingAsksForUsage:
    """Without this flag a streamed answer is uncountable, full stop.

    OpenAI-compatible endpoints omit token counts from streamed responses unless
    the request asks for them. The mock model never reaches ``ChatOpenAI``, so
    every other test in this file passes whether or not the flag is sent — which
    means the flag had **no coverage at all** until this class existed. Found by
    the reverse validation reporting "not caught": a mutation that dropped
    ``stream_usage`` broke nothing, because nothing looked.
    """

    def _real_model(self, **kwargs: Any) -> Any:
        """Build an actual ChatOpenAI. Constructing one makes no network call.

        An explicit key and a non-placeholder base URL are what stop
        `build_chat_model` from substituting the mock.
        """
        from app.runtime.ai.provider import build_chat_model

        return build_chat_model(
            {"api_key": "sk-not-a-real-key", "base_url": "https://example.invalid/v1"},
            **kwargs,
        )

    def test_a_streamed_model_is_asked_for_usage(self):
        model = self._real_model(streaming=True, stream_usage=True)

        assert model.stream_usage is True

    def test_a_non_streamed_call_can_also_ask(self):
        """`complete` uses ainvoke, but passing the flag is harmless and consistent."""
        model = self._real_model(streaming=False, stream_usage=True)

        assert model.stream_usage is True

    def test_the_default_is_off(self):
        """Off by default so the flag is a deliberate choice, not an accident.

        It changes what the provider is asked for, and a caller that never reads
        the counts should not request them.
        """
        model = self._real_model()

        assert model.stream_usage is False

    def test_the_gateway_asks_for_it_when_streaming(self):
        """The wiring, not just the parameter — this is what the mutation broke.

        Asserted by observing what the gateway passes to the factory, so a
        gateway that silently stopped requesting usage would fail here.
        """
        import app.runtime.ai.gateway as gateway_module
        from app.runtime.ai.provider import build_chat_model as real_build

        captured: list[dict[str, Any]] = []

        def spy(overrides: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            captured.append(dict(kwargs))
            return real_build(overrides, **kwargs)

        gateway = LangChainModelGateway(
            settings=_settings(
                llm_api_key="sk-not-a-real-key",
                llm_base_url="https://example.invalid/v1",
            )
        )
        original = gateway_module.build_chat_model
        gateway_module.build_chat_model = spy
        try:

            async def drain() -> None:
                async for _chunk in gateway.stream([HumanMessage(content="hi")]):
                    pass

            try:
                asyncio.run(drain())
            except Exception:  # noqa: BLE001 - the network call is expected to fail
                pass
        finally:
            gateway_module.build_chat_model = original

        assert captured, "the gateway never reached the factory"
        assert captured[0]["stream_usage"] is True


class TestCredentialsStillDoNotFollowARedirect:
    """The gateway must not become a way around the Phase 0 credential rule.

    `resolve_llm_config` withholds the operator's key when a node points at a
    different endpoint. Routing model calls through a new object is exactly the
    kind of change that could quietly reintroduce that leak.
    """

    def test_a_node_override_to_another_host_gets_no_key(self, monkeypatch):
        from app.runtime.ai.provider import resolve_llm_config

        monkeypatch.setenv("LLM_API_KEY", "sk-operator-secret")
        monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
        get_settings.cache_clear()
        settings = get_settings()

        config = resolve_llm_config({"base_url": "https://attacker.example/v1"}, settings)

        assert config["base_url"] == "https://attacker.example/v1"
        assert config["api_key"] == ""

    def test_the_gateway_passes_overrides_through_the_same_resolver(self):
        """Asserted on the gateway, not the resolver, so the wiring is pinned.

        A gateway that built its own config would leave the resolver's test
        passing while the product leaked.
        """
        from app.runtime.ai.provider import resolve_llm_config

        settings = _settings(llm_api_key="sk-operator", llm_base_url="https://api.deepseek.com/v1")
        gateway = LangChainModelGateway(settings=settings)

        seen: list[dict[str, Any]] = []
        original = resolve_llm_config

        import app.runtime.ai.gateway as gateway_module

        def spy(overrides, cfg=None):
            result = original(overrides, cfg)
            seen.append(result)
            return result

        try:
            gateway_module.build_chat_model = lambda ov, **kw: _StubModel(spy(ov, kw.get("settings")))
            asyncio.run(
                gateway.complete(
                    [HumanMessage(content="hi")],
                    overrides={"base_url": "https://attacker.example/v1"},
                )
            )
        finally:
            gateway_module.build_chat_model = (
                __import__("app.runtime.ai.provider", fromlist=["build_chat_model"]).build_chat_model
            )

        assert seen and seen[0]["api_key"] == ""


class _StubModel:
    """What `build_chat_model` returns, for a test that never reaches a provider.

    **Not a chat-model test double**, which is why `langchain_core`'s
    `fake_chat_models` family is the wrong tool here. Those exist to exercise chat
    *behaviour* — message chunking, token callbacks, tool calls — and using one
    would mean constructing a message iterator and then depending on how a real
    `BaseChatModel` renders it. This test asserts none of that: the assertion is on
    `seen`, the config the resolver produced, and the model object only has to
    satisfy the three methods `LangChainModelGateway.complete` calls on it.

    Keeping it a local stub also keeps the failure mode honest. A real fake would
    validate its own inputs, so a gateway that passed the wrong argument types
    could still pass; a duck-typed stub fails on the first unexpected call.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def bind(self, **_kwargs: Any) -> "_StubModel":
        return self

    async def ainvoke(self, _messages: Any) -> AIMessage:
        return AIMessage(content="ok")

    async def astream(self, _messages: Any):
        yield AIMessageChunk(content="ok")
