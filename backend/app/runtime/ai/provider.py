"""LLM provider abstraction.

One factory, driven entirely by settings, so the scaffold works against any
OpenAI-compatible endpoint: OpenAI, DeepSeek, Ollama, vLLM, LM Studio, ...

A deterministic :class:`MockChatModel` is used automatically when no API key is
configured. That keeps the whole product demonstrable — and the test suite
hermetic — without a network call.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Iterator, Sequence

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

from app.settings import Settings, get_settings

# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

_CONFIG_KEYS = (
    "model",
    "base_url",
    "api_key",
    "temperature",
    "timeout",
    "max_retries",
    "reasoning_effort",
)


def _is_deepseek(model: str, base_url: str) -> bool:
    return "deepseek" in f"{model} {base_url}".lower()


def _thinking_choice(value: Any, fallback: str = "auto") -> str:
    """Normalise a thinking switch from settings or a node override."""
    if isinstance(value, bool):
        return "enabled" if value else "disabled"
    text = str(value or fallback).strip().lower()
    return text if text in {"auto", "enabled", "disabled"} else fallback


def _thinking_mode(choice: str, model: str, base_url: str) -> str:
    """Resolve ``auto`` against the endpoint, or ``""`` when unsupported.

    DeepSeek's thinking mode is on by default, so ``auto`` means "enabled" for a
    DeepSeek model. Other OpenAI-compatible endpoints (OpenAI, Ollama, vLLM) do
    not accept a ``thinking`` body field, so auto leaves them untouched.
    """
    choice = _thinking_choice(choice)
    if choice == "auto":
        return "enabled" if _is_deepseek(model, base_url) else ""
    return choice


def _api_key_for(settings: Settings) -> str:
    """The operator's key, from wherever this deployment keeps it.

    The subtlety worth spelling out: ``resolve_llm_config`` accepts an explicit
    ``settings`` argument, and the credential has to keep honouring it. It does,
    but only when the deployment *is* settings-backed — asking a secrets file
    deployment to prefer a passed-in ``Settings`` object would be a way to
    override a configured credential from a call site, which is the hole this
    module exists to close.

    So: ``source == "settings"`` means "the passed settings are the source", and
    anything else means the configured provider is. Both branches answer the
    same question — where does this deployment keep its key — and neither lets a
    caller substitute a different one.
    """
    from app.security.secrets import SECRET_LLM_API_KEY, get_secret_provider

    provider = get_secret_provider()
    if provider.source == "settings":
        return settings.llm_api_key or ""
    return provider.get(SECRET_LLM_API_KEY) or ""


def resolve_llm_config(
    overrides: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Merge global settings with a per-node override.

    Node-level overrides exist so one workflow can mix a cheap model for routing
    with an expensive one for the final answer.

    **Credentials do not follow a redirect.** If a node points at a different
    endpoint than the global one, the global API key is withheld unless the node
    supplies its own. Without this, a workflow could set
    ``model.base_url = "https://attacker/v1"`` and receive the operator's
    ``LLM_API_KEY`` in an ``Authorization: Bearer`` header — no Code node, no
`trusted` flag, just a config value. See `docs/reference/backend-layers.md#l9`.

    The credential itself comes from :func:`app.security.secrets.get_secret_provider`
    so a deployment can source it from somewhere other than ``.env``. The
    redirect rule stays *here* rather than moving into the provider, because it
    compares the key against an endpoint — and a secrets module has no business
    knowing what an endpoint is.
    """
    settings = settings or get_settings()

    base: dict[str, Any] = {
        "model": settings.llm_model,
        "base_url": settings.llm_base_url,
        "api_key": _api_key_for(settings),
        "temperature": settings.llm_temperature,
        "timeout": settings.llm_timeout,
        "max_retries": settings.llm_max_retries,
        "reasoning_effort": settings.llm_reasoning_effort,
    }
    choice = _thinking_choice(settings.llm_thinking)

    if overrides:
        for key in _CONFIG_KEYS:
            value = overrides.get(key)
            if value not in (None, ""):
                base[key] = value
        if "thinking" in overrides:
            choice = _thinking_choice(overrides.get("thinking"), choice)

        requested_url = overrides.get("base_url")
        if requested_url and requested_url != settings.llm_base_url:
            # A different endpoint is a different trust domain. Keep the model
            # override, drop the credential.
            base["api_key"] = overrides.get("api_key") or ""

    base["thinking"] = _thinking_mode(choice, base["model"], base["base_url"])
    effort = str(base.get("reasoning_effort") or "high").strip().lower()
    base["reasoning_effort"] = effort if effort in {"low", "high", "max"} else "high"
    return base


def _is_placeholder_endpoint(base_url: str) -> bool:
    return "api.openai.com" in (base_url or "")


# ---------------------------------------------------------------------------
# Mock model (no network)
# ---------------------------------------------------------------------------


class MockChatModel(BaseChatModel):
    """Deterministic stand-in used when no API key is configured.

    It echoes a short, clearly-labelled summary of the prompt so that wiring,
    streaming, run history, and the whole UI can be exercised offline.
    """

    model_name: str = "mock-model"
    label: str = "[mock-llm]"

    @property
    def _llm_type(self) -> str:
        return "mock-chat-model"

    # -- helpers -----------------------------------------------------------
    def _summarise(self, messages: Sequence[BaseMessage]) -> str:
        last = ""
        for message in reversed(messages):
            content = getattr(message, "content", "")
            if isinstance(content, str) and content.strip():
                last = content.strip()
                break
            if isinstance(content, list):
                last = " ".join(str(part) for part in content).strip()
                break
        excerpt = last.replace("\n", " ")[:220]
        return (
            f"{self.label} No API key configured, so this is a simulated reply. "
            f"Prompt received ({len(messages)} message(s)): {excerpt or '<empty>'}"
        )

    # -- sync --------------------------------------------------------------
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = self._summarise(messages)
        message = AIMessage(content=text, response_metadata={"mock": True})
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        for token in self._summarise(messages).split(" "):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=token + " "))
            if run_manager:
                run_manager.on_llm_new_token(token + " ")
            yield chunk

    # -- async -------------------------------------------------------------
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        await asyncio.sleep(0)
        return self._generate(messages, stop, None, **kwargs)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for token in self._summarise(messages).split(" "):
            yield ChatGenerationChunk(message=AIMessageChunk(content=token + " "))


# ---------------------------------------------------------------------------
# Native reasoning
# ---------------------------------------------------------------------------


def _reasoning_delta(chunk: dict[str, Any]) -> str:
    """Read DeepSeek's ``reasoning_content`` out of one streamed delta.

    ``langchain-openai`` 1.6 converts the delta to an ``AIMessageChunk`` and
    silently drops fields it does not know, so native reasoning never reaches a
    caller. We keep the text here and attach it to the chunk instead.
    """
    choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    value = delta.get("reasoning_content")
    return value if isinstance(value, str) else ""


def _reasoning_message(response: Any) -> str:
    """Read native reasoning out of a complete (non-streamed) response."""
    if isinstance(response, dict):
        choices = response.get("choices") or []
        raw = (choices[0] or {}).get("message") if choices else None
    else:
        choices = getattr(response, "choices", None) or []
        raw = getattr(choices[0], "message", None) if choices else None
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump()
    if isinstance(raw, dict):
        value = raw.get("reasoning_content")
        return value if isinstance(value, str) else ""
    return ""


class ReasoningChatOpenAI(ChatOpenAI):
    """``ChatOpenAI`` that preserves provider-native reasoning.

    Two things are done here that the base class does not do:

    * streamed and complete ``reasoning_content`` are attached to
      ``additional_kwargs`` so the UI can show them;
    * on a follow-up request, an assistant message's ``reasoning_content`` is
      sent back. DeepSeek requires this once a tool call is involved; without it
      the API answers HTTP 400.
    """

    #: Set when this model was constructed with thinking enabled.
    native_reasoning: bool = False

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is not None:
            reasoning = _reasoning_delta(chunk)
            if reasoning:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return generation

    def _create_chat_result(
        self, response: dict | Any, generation_info: dict | None = None
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        reasoning = _reasoning_message(response)
        if reasoning and result.generations:
            result.generations[0].message.additional_kwargs[
                "reasoning_content"
            ] = reasoning
        return result

    def _get_request_payload(
        self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if not self.native_reasoning:
            return payload

        messages = self._convert_input(input_).to_messages()
        converted = payload.get("messages") or []
        for source, target in zip(messages, converted):
            if not isinstance(source, AIMessage) or not isinstance(target, dict):
                continue
            reasoning = source.additional_kwargs.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                target["reasoning_content"] = reasoning
        return payload


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_chat_model(
    overrides: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    streaming: bool = True,
    tools: list[Any] | None = None,
    force_mock: bool = False,
    stream_usage: bool = False,
) -> BaseChatModel:
    """Build a chat model for a node or for an AI helper call.

    Falls back to :class:`MockChatModel` when credentials are missing, so a
    first-time user can click Run and see something happen.

    ``stream_usage`` asks the provider to include token counts on streamed
    responses. OpenAI-compatible APIs omit them by default, which makes a
    streamed answer impossible to cost — see :mod:`app.runtime.ai.gateway`. Off by
    default because it is only useful to a caller that reads the counts.
    """
    config = resolve_llm_config(overrides, settings)
    use_mock = force_mock or (
        not config["api_key"] and _is_placeholder_endpoint(config["base_url"])
    )
    if use_mock:
        return MockChatModel()

    thinking = config.get("thinking") or ""
    request: dict[str, Any] = {}
    if thinking == "enabled":
        # DeepSeek's documented switch. Reasoning effort only applies while
        # thinking is on; temperature is ignored by the provider in this mode.
        request["extra_body"] = {"thinking": {"type": "enabled"}}
        request["reasoning_effort"] = config.get("reasoning_effort") or "high"
    elif thinking == "disabled" and _is_deepseek(
        config["model"], config["base_url"]
    ):
        request["extra_body"] = {"thinking": {"type": "disabled"}}

    model = ReasoningChatOpenAI(
        model=config["model"],
        base_url=config["base_url"] or None,
        api_key=config["api_key"] or "not-needed",
        temperature=config["temperature"],
        timeout=config["timeout"],
        max_retries=config["max_retries"],
        streaming=streaming,
        stream_usage=stream_usage,
        native_reasoning=thinking == "enabled",
        **request,
    )
    if tools:
        return model.bind_tools(tools)
    return model


def is_mock_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    # Through the resolver rather than reading the setting directly: a
    # deployment that sources its key from a secrets file has a key, and a
    # direct read of `settings.llm_api_key` would report the mock model while a
    # real model was in use.
    config = resolve_llm_config(None, settings)
    return not config["api_key"] and _is_placeholder_endpoint(config["base_url"])


def dumps(value: Any, *, limit: int = 2000) -> str:
    """Compact, truncated JSON — used when embedding values into prompts."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str, indent=2)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


__all__ = [
    "MockChatModel",
    "build_chat_model",
    "dumps",
    "is_mock_configured",
    "resolve_llm_config",
]
