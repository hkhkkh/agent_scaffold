"""Minimal, dependency-free template rendering for Prompt nodes.

Supports two placeholder forms::

    {{ query }}                 # a top-level shared-state key
    {{ vars.node_1.out }}       # any dotted path into state
    {{ inputs.in }}             # value arriving on an input handle

Unknown paths render as an empty string and are reported through the optional
``on_missing`` callback so the Prompt node can surface a run warning instead of
silently sending a half-built prompt.
"""

from __future__ import annotations

import re
from typing import Any, Callable

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<missing>"


MISSING = _Missing()


def lookup(path: str, scope: dict[str, Any]) -> Any:
    """Resolve a dotted path, returning :data:`MISSING` when absent."""
    current: Any = scope
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return MISSING
            current = current[part]
        elif isinstance(current, (list, tuple)):
            if not part.isdigit():
                return MISSING
            index = int(part)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def find_placeholders(template: str) -> list[str]:
    return _PLACEHOLDER.findall(template or "")


def render(
    template: str,
    scope: dict[str, Any],
    *,
    on_missing: Callable[[str], None] | None = None,
) -> str:
    """Substitute ``{{ path }}`` placeholders using ``scope``."""
    if not template:
        return ""

    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        value = lookup(path, scope)
        if value is MISSING:
            if on_missing:
                on_missing(path)
            return ""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list, tuple)):
            import json

            try:
                return json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    return _PLACEHOLDER.sub(replace, template)


__all__ = ["MISSING", "find_placeholders", "lookup", "render"]
