"""AI helpers that fill in a node's configuration.

The core operation of the whole scaffold: the user writes "here is what this step
should do" in plain language, and the model turns that into typed config values.

Two design commitments make this trustworthy rather than magical:

* **Suggestions are validated against the node type's declared params.** A model
  cannot introduce a field that does not exist, so the Inspector never shows
  something it cannot edit.
* **Nothing is applied automatically.** Every result comes back as a diff the
  user accepts or rejects. Silent canvas mutation would make the graph
  unpredictable, which is the opposite of what a visual editor is for.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.runtime.ai.messages import Language, render
from app.ai_buildtime.metering import record_reply_usage
from app.ai_buildtime.parsing import require_object
from app.ai_buildtime.prompts import (
    build_code_to_node_messages,
    build_fill_node_messages,
    build_node_to_code_messages,
)
from app.runtime.ai.provider import build_chat_model, resolve_llm_config
from app.graph.schema import NodeSpec, WorkflowGraph
from app.nodes import get_node_type_spec, has_node_type


class AINotConfigured(RuntimeError):
    """Raised when the AI features are disabled or unavailable."""


class AIError(RuntimeError):
    """Raised when the model's response cannot be used."""


# ---------------------------------------------------------------------------
#: Sentences this module authors itself, as opposed to the ones the model writes.
#: Kept in one table so a new message cannot ship in a single language — the same
#: contract `app/ai/repair.py` uses, over the same renderer.
_MESSAGES: dict[str, dict[str, str]] = {
    "rejected_fields": {
        "en": "Ignored fields that this node type does not have: {fields}",
        "zh": "已忽略该节点类型没有的字段：{fields}",
    },
}


def _t(key: str, lang: str, **vars: Any) -> str:
    """This module's own sentences, rendered. See `app/runtime/ai/messages.py`."""
    return render(_MESSAGES, key, lang, **vars)


# ---------------------------------------------------------------------------
# Fill a node's configuration
# ---------------------------------------------------------------------------


async def fill_node(
    graph: WorkflowGraph,
    node: NodeSpec,
    *,
    extra_instruction: str = "",
    lang: Language = "en",
) -> dict[str, Any]:
    """Suggest config values for ``node`` based on its description.

    Returns ``{config, rationale, warnings, applied_to}`` where ``config`` holds
    only validated keys, ready for the user to accept.
    """
    if not has_node_type(node.type):
        raise AIError(f"Unknown node type '{node.type}'.")

    spec = get_node_type_spec(node.type)
    messages = build_fill_node_messages(graph, node)
    if extra_instruction.strip():
        messages.append(
            {
                "role": "user",
                "content": f"Additional instruction from the user: {extra_instruction}",
            }
        )

    content = await _complete(messages)
    payload = require_object(content, context="the node suggestion")

    raw_config = payload.get("config")
    if not isinstance(raw_config, dict):
        raise AIError("The model did not return a `config` object.")

    cleaned, rejected = _validate_config(spec, raw_config)

    warnings = [str(w) for w in payload.get("warnings") or [] if str(w).strip()]
    if rejected:
        # The only sentence this module authors; everything else in `warnings`
        # comes from the model, in whichever language the user asked in.
        warnings.append(_t("rejected_fields", lang, fields=", ".join(sorted(rejected))))

    return {
        "config": cleaned,
        "rationale": str(payload.get("rationale") or "").strip(),
        "warnings": warnings,
        "changes": _diff(node.config or {}, cleaned),
    }


def _validate_config(
    spec: Any, raw: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Keep only keys the node type declares, and coerce values to the right type.

    Dropping unknown keys is what keeps the suggestion honest: the Inspector can
    always render the result.
    """
    allowed = {param.name: param for param in spec.params}
    cleaned: dict[str, Any] = {}
    rejected: list[str] = []

    for key, value in raw.items():
        param = allowed.get(key)
        if param is None:
            rejected.append(key)
            continue
        coerced = _coerce(param, value)
        if coerced is not _SKIP:
            cleaned[key] = coerced

    return cleaned, rejected


_SKIP: Any = object()


def _coerce(param: Any, value: Any) -> Any:
    """Best-effort conversion of a model-supplied value to the param's type.

    Models routinely return `"3"` for a number or `"true"` for a boolean. Failing
    on those would make the feature feel broken over something trivially fixable.
    """
    if value is None:
        return _SKIP

    try:
        if param.type == "number":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, str):
                return float(value) if "." in value else int(value)
            return value

        if param.type == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes", "on")
            return bool(value)

        if param.type == "string":
            if isinstance(value, str):
                enum = getattr(param, "enum", None)
                if enum and value not in enum:
                    return _SKIP
                return value
            return str(value)

        if param.type == "select":
            if isinstance(value, str):
                options = {option.value for option in param.options}
                # Accept a label as well as a value — models mix them up.
                labels = {option.label.lower(): option.value for option in param.options}
                if value in options:
                    return value
                return labels.get(value.strip().lower(), _SKIP)
            return _SKIP

        if param.type in ("json", "model", "code"):
            if param.type == "code" and isinstance(value, str):
                return value
            return value
    except (TypeError, ValueError):
        return _SKIP

    return value


def _diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Produce a reviewable list of changes for the UI."""
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old == new:
            continue
        changes.append({"field": key, "before": old, "after": new, "kind": "changed"})
    return changes


# ---------------------------------------------------------------------------
# Node <-> code conversion
# ---------------------------------------------------------------------------


async def node_to_code(graph: WorkflowGraph, node: NodeSpec) -> dict[str, Any]:
    """Convert a node's configuration into an equivalent Code-node snippet."""
    if node.type == "code":
        raise AIError("This node is already a Code node.")

    messages = build_node_to_code_messages(graph, node)
    payload = require_object(await _complete(messages), context="the code conversion")

    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        raise AIError("The model did not return any code.")

    language = str(payload.get("language") or "python")
    if language.lower() not in ("python", "py"):
        raise AIError(
            f"Only Python conversions are supported; the model returned '{language}'."
        )

    return {
        "code": code,
        "notes": str(payload.get("notes") or "").strip(),
        # Surfaced prominently in the UI: a lossy conversion must not look exact.
        "lossy": bool(payload.get("lossy", False)),
    }


async def code_to_node(
    graph: WorkflowGraph,
    node: NodeSpec,
    *,
    code: str,
    target_type: str,
) -> dict[str, Any]:
    """Convert a Code node's snippet into configuration for another node type."""
    if not has_node_type(target_type):
        raise AIError(f"Unknown node type '{target_type}'.")
    if target_type == "code":
        raise AIError("Target type is already Code; nothing to convert.")

    spec = get_node_type_spec(target_type)
    messages = build_code_to_node_messages(graph, node, code, target_type)
    payload = require_object(await _complete(messages), context="the node conversion")

    raw_config = payload.get("config")
    if not isinstance(raw_config, dict):
        raise AIError("The model did not return a `config` object.")

    cleaned, rejected = _validate_config(spec, raw_config)
    warnings = [str(w) for w in payload.get("warnings") or [] if str(w).strip()]
    if rejected:
        warnings.append(
            "Ignored fields that this node type does not have: "
            + ", ".join(sorted(rejected))
        )

    return {
        "config": cleaned,
        "rationale": str(payload.get("rationale") or "").strip(),
        "warnings": warnings,
        "equivalent": bool(payload.get("equivalent", True)),
    }


# ---------------------------------------------------------------------------
# Model call
# ---------------------------------------------------------------------------


async def _complete_message(messages: list[dict[str, str]]) -> Any:
    """Call the configured model once and return the response message.

    Separate from `_complete` so callers that want the model's reasoning (the
    copilot shows it to the user) do not have to call the model a second time.
    """
    from app.settings import get_settings

    settings = get_settings()
    if not settings.ai_enabled:
        raise AINotConfigured(
            "AI features are disabled. Set AI_ENABLED=true in .env to use them."
        )

    config = resolve_llm_config(None, settings)
    model = build_chat_model(
        {"temperature": 0.0, "max_tokens": 2048}, settings=settings, streaming=False
    )

    if getattr(model, "model_name", "") == "mock-model":
        raise AINotConfigured(
            "No LLM API key is configured, so AI features are unavailable. "
            f"Set LLM_API_KEY in .env (currently pointing at {config['base_url']}), "
            "then restart the backend."
        )

    response = await model.ainvoke(messages)  # type: ignore[arg-type]
    record_reply_usage(response)
    return response


async def _stream_message(
    messages: list[dict[str, str]],
    *,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    on_reasoning: Callable[[str], Awaitable[None]] | None = None,
) -> Any:
    """Stream one reply and return it in the same shape as `_complete_message`.

    Authoring replies are JSON, so the useful text cannot be parsed until the
    object closes; the caller decides what to do with each delta (the copilot
    extracts the `answer` field as it arrives). Usage is read from the final
    chunk, which is why `stream_usage=True` is set.
    """
    from types import SimpleNamespace

    from app.settings import get_settings

    settings = get_settings()
    if not settings.ai_enabled:
        raise AINotConfigured(
            "AI features are disabled. Set AI_ENABLED=true in .env to use them."
        )

    config = resolve_llm_config(None, settings)
    model = build_chat_model(
        {"temperature": 0.0, "max_tokens": 2048},
        settings=settings,
        streaming=True,
        stream_usage=True,
    )
    if getattr(model, "model_name", "") == "mock-model":
        raise AINotConfigured(
            "No LLM API key is configured, so AI features are unavailable. "
            f"Set LLM_API_KEY in .env (currently pointing at {config['base_url']}), "
            "then restart the backend."
        )

    parts: list[str] = []
    reasoning: list[str] = []
    usage: dict[str, Any] | None = None
    async for chunk in model.astream(messages):  # type: ignore[arg-type]
        delta = _message_text(chunk)
        if delta:
            parts.append(delta)
            if on_text is not None:
                await on_text(delta)
        # Streamed reasoning is token fragments, not finished blocks: do not
        # strip them or every space between words is lost.
        thought = _message_reasoning(chunk, strip=False)
        if thought:
            reasoning.append(thought)
            if on_reasoning is not None:
                await on_reasoning(thought)
        metadata = getattr(chunk, "usage_metadata", None)
        if isinstance(metadata, dict) and metadata:
            usage = metadata

    response = SimpleNamespace(
        content="".join(parts),
        additional_kwargs=({"reasoning_content": "".join(reasoning)} if reasoning else {}),
        response_metadata={},
        usage_metadata=usage,
    )
    record_reply_usage(response)
    return response


def _message_text(response: Any) -> str:
    content = getattr(response, "content", "")
    if isinstance(content, list):
        content = "".join(
            part
            if isinstance(part, str)
            else str(part.get("text", ""))
            if isinstance(part, dict) and part.get("type") not in {"reasoning", "thinking"}
            else ""
            for part in content
        )
    return str(content)


def _message_reasoning(response: Any, *, strip: bool = True) -> str:
    """Whatever the provider exposed as this turn's reasoning, if anything.

    DeepSeek puts it in `additional_kwargs["reasoning_content"]`; other
    OpenAI-compatible providers use `reasoning` or a content block. A model
    without a reasoning mode returns nothing, which is not an error.

    ``strip=False`` is for stream deltas. A chunk is often just ``" "`` or
    ``" the"``; stripping it would glue the words together on screen.
    """
    def clean(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip() if strip else value

    additional = getattr(response, "additional_kwargs", {}) or {}
    metadata = getattr(response, "response_metadata", {}) or {}
    for key in ("reasoning_content", "reasoning", "thinking"):
        text = clean(additional.get(key) or metadata.get(key))
        if text:
            return text

    content = getattr(response, "content", None)
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"reasoning", "thinking"}:
                text = clean(part.get("text") or part.get("reasoning") or "")
                if text:
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)
    return ""


async def _complete(messages: list[dict[str, str]]) -> str:
    """Call the configured model once and return its text."""
    return _message_text(await _complete_message(messages))


__all__ = [
    "AIError",
    "AINotConfigured",
    "code_to_node",
    "fill_node",
    "node_to_code",
]
