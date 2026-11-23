"""Native reasoning across the provider seam.

DeepSeek's ``deepseek-flash`` has thinking mode on by default. ``langchain-openai``
drops ``reasoning_content`` while converting stream deltas, so the application
has to preserve it explicitly. These tests pin the request parameters, the
stream/complete extraction, and the tool-call round trip that DeepSeek rejects
without ``reasoning_content``.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from app.ai_buildtime.gen_node import _message_reasoning
from app.runtime.ai.provider import (
    ReasoningChatOpenAI,
    build_chat_model,
    resolve_llm_config,
)
from app.settings import Settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_model": "deepseek-flash",
        "llm_base_url": "https://api.deepseek.com/v1",
        "ai_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_auto_enables_thinking_for_deepseek_only():
    deepseek = resolve_llm_config(None, _settings())
    assert deepseek["thinking"] == "enabled"
    assert deepseek["reasoning_effort"] == "high"

    openai = resolve_llm_config(
        None,
        _settings(llm_model="gpt-4o-mini", llm_base_url="https://api.openai.com/v1"),
    )
    assert openai["thinking"] == ""


def test_override_can_disable_and_force_effort():
    config = resolve_llm_config(
        {"thinking": "disabled", "reasoning_effort": "max"}, _settings()
    )
    assert config["thinking"] == "disabled"
    assert config["reasoning_effort"] == "max"


def test_build_model_sends_deepseek_thinking_parameters():
    model = build_chat_model(
        None, settings=_settings(), streaming=True, stream_usage=True
    )
    assert isinstance(model, ReasoningChatOpenAI)
    assert model.native_reasoning is True
    assert model.extra_body == {"thinking": {"type": "enabled"}}
    assert model.reasoning_effort == "high"


def test_disabled_thinking_is_sent_to_deepseek():
    model = build_chat_model(
        {"thinking": "disabled"}, settings=_settings(), streaming=False
    )
    assert model.native_reasoning is False
    assert model.extra_body == {"thinking": {"type": "disabled"}}
    assert model.reasoning_effort is None


def test_non_deepseek_model_gets_no_extra_body():
    model = build_chat_model(
        None,
        settings=_settings(
            llm_model="llama3.1", llm_base_url="http://127.0.0.1:11434/v1"
        ),
        streaming=False,
    )
    assert model.extra_body is None
    assert model.reasoning_effort is None


def test_stream_delta_keeps_reasoning_and_whitespace():
    model = build_chat_model(None, settings=_settings(), streaming=True)
    chunk = {
        "id": "chunk",
        "model": "deepseek-flash",
        "choices": [
            {"index": 0, "delta": {"reasoning_content": " need"}, "finish_reason": None}
        ],
    }
    generation = model._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)
    assert generation is not None
    message = generation.message
    assert message.additional_kwargs["reasoning_content"] == " need"
    assert _message_reasoning(message, strip=False) == " need"


def test_complete_response_keeps_reasoning():
    model = build_chat_model(None, settings=_settings(), streaming=False)
    result = model._create_chat_result(
        {
            "id": "x",
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "9.8",
                        "reasoning_content": "9.11 < 9.8",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    assert result.generations[0].message.additional_kwargs["reasoning_content"] == (
        "9.11 < 9.8"
    )


def test_tool_round_trip_sends_reasoning_back():
    model = build_chat_model(None, settings=_settings(), streaming=False)
    assistant = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "use the tool"},
        tool_calls=[
            {"name": "add", "args": {"a": 1, "b": 2}, "id": "call_1", "type": "tool_call"}
        ],
    )
    messages = [
        {"role": "user", "content": "add"},
        assistant,
        ToolMessage(content="3", tool_call_id="call_1"),
    ]
    payload = model._get_request_payload(messages)
    assert payload["messages"][1]["reasoning_content"] == "use the tool"


def test_disabled_thinking_does_not_resend_reasoning():
    model = build_chat_model(
        {"thinking": "disabled"}, settings=_settings(), streaming=False
    )
    assistant = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "old"},
        tool_calls=[
            {"name": "add", "args": {"a": 1, "b": 2}, "id": "call_1", "type": "tool_call"}
        ],
    )
    payload = model._get_request_payload(
        [{"role": "user", "content": "add"}, assistant]
    )
    assert "reasoning_content" not in payload["messages"][1]
