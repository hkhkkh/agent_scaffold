"""Tolerant JSON extraction from model responses.

Models wrap JSON in prose, in ```json fences, or in both. A strict
``json.loads`` turns that into a failed feature, so this module finds the JSON
inside whatever else came back.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json|jsonc)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any | None:
    """Best-effort extraction of a JSON value from ``text``.

    Tries, in order: the whole string, a fenced block, then the outermost
    ``{...}`` or ``[...]``. Returns ``None`` when nothing parses, so callers can
    decide whether to fall back or report an error.
    """
    if not text or not text.strip():
        return None

    candidate = text.strip()

    # 1. Already pure JSON.
    parsed = _try_load(candidate)
    if parsed is not None:
        return parsed

    # 2. Inside a markdown fence.
    for match in _FENCE.finditer(candidate):
        parsed = _try_load(match.group(1).strip())
        if parsed is not None:
            return parsed

    # 3. Outermost braces / brackets, scanning for a balanced span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    parsed = _try_load(candidate[start : index + 1])
                    if parsed is not None:
                        return parsed
                    break

    return None


def require_object(text: str, *, context: str = "the model response") -> dict[str, Any]:
    """Extract a JSON object or raise a message worth showing to a user."""
    value = extract_json(text)
    if value is None:
        excerpt = (text or "").strip()[:400]
        raise ValueError(
            f"Could not find JSON in {context}. Raw text began with: {excerpt!r}"
        )
    if not isinstance(value, dict):
        raise ValueError(
            f"Expected a JSON object in {context} but found {type(value).__name__}."
        )
    return value


def _try_load(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


__all__ = ["extract_json", "require_object"]
