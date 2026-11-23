"""Charge build-time AI calls to the public visitor quota.

Runtime model calls go through ``runtime/ai/gateway.py`` and report usage there.
The authoring paths (generate / repair / copilot / fill) construct a LangChain
model directly, so they need the same accounting at their own call sites. This
module is platform-side: exported agents never include ``ai_buildtime``.
"""

from __future__ import annotations

from typing import Any

from app.runtime.ai.gateway import ModelUsage, usage_from_message
from app.security.quotas import record_usage


def record_reply_usage(response: Any) -> ModelUsage | None:
    """Report one authoring reply's token usage; a no-op outside public mode.

    Returns what the provider reported so the streaming caller can show a live
    total. ``None`` means the provider did not report usage; the caller should
    keep saying "not reported" rather than display a confident zero.
    """
    usage = usage_from_message(response)
    record_usage(usage or ModelUsage(calls=1))
    return usage


def reply_usage(response: Any) -> ModelUsage | None:
    """Read usage without recording it (the call site already recorded)."""
    return usage_from_message(response)


__all__ = ["record_reply_usage", "reply_usage"]
