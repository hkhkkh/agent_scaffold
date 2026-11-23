"""Serialisation helpers for events, run records, and the transcript.

Three problems this solves, all of which bite the moment a run produces anything
interesting:

1. **Node outputs are not JSON.** An LLM node returns ``AIMessage`` objects and
   ``datetime`` values. JSON-encoding those raises deep inside the event bus, at
   which point the run has already happened and the error is pure noise.
   :func:`jsonable` converts anything to JSON-safe primitives, with cycle
   protection and size caps.

2. **Run payloads contain credentials.** A node's resolved config can hold an API
   key. Streaming that to the browser or writing it to disk would leak it into
   the transcript. :func:`redact` masks values under secret-looking keys.

3. **A run's answer has no name.** An End node returns ``{"<output_label>":
   value}``, so the label is already a key on the payload — but every consumer
   that wants "the result" still has to agree on which key that is.
   :func:`final_output` is that agreement, written once.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import enum
import json
import re
import uuid
from pathlib import Path
from typing import Any

#: Substrings that mark a config key as sensitive. Matched case-insensitively.
SECRET_HINTS = (
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "authorization",
    "auth",
    "private_key",
    "access_key",
)

#: Keys whose values pass through untouched even if they look secret-ish. Without
#: this, `auth_mode: "oauth"` would be masked into uselessness.
NOT_SECRET = frozenset({"auth_type", "auth_mode", "authorization_type", "token_count"})

MASK = "***"

#: Default caps. Generous enough for debugging, small enough that a run
#: transcript stays openable in an editor.
DEFAULT_MAX_STRING = 20_000
DEFAULT_MAX_ITEMS = 200
DEFAULT_MAX_DEPTH = 8

# ---------------------------------------------------------------------------
# Content-level secrets
# ---------------------------------------------------------------------------
#
# `looks_secret` above works on **key names**. That leaves a documented gap: a
# credential pasted into a prompt *string* is not under a secret key, so it
# survives — and a prompt that templates a key into its output carries the real
# value into a step payload on its way to disk.
#
# Closing it needs content matching, and upstream already ships the engine:
# `langchain.agents.middleware.pii.apply_strategy` turns a list of matches into
# rewritten text for a named strategy (`redact` / `mask` / `hash` / `block`),
# and `PIIMatch` is just `{type, value, start, end}`. What it does not ship is a
# detector for API keys — its built-ins are email, credit_card, ip, mac_address,
# and url. So we take the engine and supply our own patterns.
#
# Every pattern below is **prefixed**, which is what keeps false positives near
# zero: `sk-`, `AKIA`, `ghp_`, `xoxb-`, and a JWT's `eyJ` header. There is
# deliberately no generic "long random string" rule — that would rewrite UUIDs,
# hashes, and base64 payloads, and a redactor that mangles ordinary data is one
# an operator turns off.
CONTENT_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # OpenAI / DeepSeek / Anthropic-style keys.
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    # GitHub tokens.
    ("api_key", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    # Slack tokens.
    ("api_key", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    # AWS access key ids.
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # JWTs, by their base64url `{"` header.
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"
        ),
    ),
)

#: Longest string the content pass will scan. A credential in a megabyte of model
#: output is not the threat model; a regex over every large string in every run is
#: a real cost, so the pass stays bounded.
MAX_SCANNED_STRING = 20_000


def find_content_secrets(text: str) -> list[dict[str, Any]]:
    """Locate secret-shaped substrings. Returns ``PIIMatch``-shaped dicts.

    The shape is upstream's, not ours, because :func:`apply_strategy` consumes
    it — matching their contract is what lets us use their rewriter.
    """
    matches: list[dict[str, Any]] = []
    for kind, pattern in CONTENT_SECRET_PATTERNS:
        for found in pattern.finditer(text):
            matches.append(
                {
                    "type": kind,
                    "value": found.group(0),
                    "start": found.start(),
                    "end": found.end(),
                }
            )
    return matches


def redact_content(text: str, *, strategy: str = "redact") -> str:
    """Mask secret-shaped substrings inside a string.

    Delegates the rewrite to upstream's ``apply_strategy`` so the strategies mean
    what they mean everywhere else in the ecosystem, rather than being our own
    re-implementation with subtly different semantics.
    """
    if not text or len(text) > MAX_SCANNED_STRING:
        return text
    matches = find_content_secrets(text)
    if not matches:
        return text
    from langchain.agents.middleware.pii import apply_strategy

    return apply_strategy(text, matches, strategy)  # type: ignore[arg-type]


def looks_secret(key: str) -> bool:
    lowered = key.lower()
    if lowered in NOT_SECRET:
        return False
    return any(hint in lowered for hint in SECRET_HINTS)


def redact(
    value: Any, *, _depth: int = 0, content: bool | None = None
) -> Any:
    """Mask credentials — by key name, and inside strings.

    Walks nested dicts and lists so a credential nested inside a config object is
    still caught. String values also get a content pass, which is what covers a
    credential that was templated into a prompt rather than stored under a
    secret-looking key.

    ``content`` overrides the content pass for one call; ``None`` (the default)
    reads :func:`content_redaction_enabled`, so a deployment can turn it off
    without every call site changing.
    """
    if _depth > DEFAULT_MAX_DEPTH:
        return value
    scan_content = content_redaction_enabled() if content is None else content
    if isinstance(value, str):
        return redact_content(value) if scan_content else value
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and looks_secret(key) and item not in (None, ""):
                out[key] = MASK
            else:
                out[key] = redact(item, _depth=_depth + 1, content=scan_content)
        return out
    if isinstance(value, (list, tuple, set)):
        return [redact(item, _depth=_depth + 1, content=scan_content) for item in value]
    return value


def content_redaction_enabled() -> bool:
    """Whether string contents are scanned for credential shapes.

    Reads the setting lazily and tolerates its absence, so this module stays
    importable without the application's configuration — which matters because
    the compiler and the API layer both import it, and neither owns settings.

    Defaults to **on**: this closes a leak that was documented as a known
    limitation, and a redactor that is off by default is one nobody notices is
    off. It is also cheap — five prefixed regexes, skipped entirely for strings
    with no match and for strings over ``MAX_SCANNED_STRING``.
    """
    try:
        from app.settings import get_settings

        return bool(getattr(get_settings(), "redact_content", True))
    except Exception:  # noqa: BLE001 - no configuration available is not an error
        return True


def jsonable(
    value: Any,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_string: int = DEFAULT_MAX_STRING,
    max_items: int = DEFAULT_MAX_ITEMS,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> Any:
    """Convert ``value`` into something ``json.dumps`` accepts.

    Handles the types that actually show up in a run: dataclasses, Pydantic
    models, LangChain messages, datetimes, enums, UUIDs, Paths, sets, and bytes.
    Cycles are broken and oversized values truncated rather than raising — a
    truncated debug payload is far more useful than a failed run.
    """
    seen = _seen if _seen is not None else set()

    if value is None or isinstance(value, (bool, int, float)):
        # NaN and Infinity are not valid JSON.
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return str(value)
        return value

    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + "\n… (truncated)"

    if isinstance(value, bytes):
        try:
            return jsonable(value.decode("utf-8", errors="replace"), max_string=max_string)
        except Exception:  # noqa: BLE001
            return f"<{len(value)} bytes>"

    if _depth >= max_depth:
        return _summarise(value)

    # Cycle guard for containers only.
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        identity = id(value)
        if identity in seen:
            return "<recursive>"
        seen = seen | {identity}

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                out["…"] = f"({len(value) - max_items} more keys)"
                break
            out[str(key)] = jsonable(
                item,
                max_depth=max_depth,
                max_string=max_string,
                max_items=max_items,
                _depth=_depth + 1,
                _seen=seen,
            )
        return out

    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        converted = [
            jsonable(
                item,
                max_depth=max_depth,
                max_string=max_string,
                max_items=max_items,
                _depth=_depth + 1,
                _seen=seen,
            )
            for item in items[:max_items]
        ]
        if len(items) > max_items:
            converted.append(f"… ({len(items) - max_items} more items)")
        return converted

    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()

    if isinstance(value, dt.timedelta):
        return value.total_seconds()

    if isinstance(value, (uuid.UUID, Path)):
        return str(value)

    if isinstance(value, enum.Enum):
        return value.value

    if isinstance(value, decimal.Decimal):
        return float(value)

    # Pydantic v2 models
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return jsonable(
                dump(mode="json"),
                max_depth=max_depth,
                max_string=max_string,
                max_items=max_items,
                _depth=_depth + 1,
                _seen=seen,
            )
        except Exception:  # noqa: BLE001 - fall through to other strategies
            pass

    # LangChain messages expose their meaningful content this way.
    lc_dict = getattr(value, "model_dump", None) or getattr(value, "dict", None)
    if callable(lc_dict):
        try:
            return jsonable(
                lc_dict(),
                max_depth=max_depth,
                max_string=max_string,
                max_items=max_items,
                _depth=_depth + 1,
                _seen=seen,
            )
        except Exception:  # noqa: BLE001
            pass

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(
            dataclasses.asdict(value),
            max_depth=max_depth,
            max_string=max_string,
            max_items=max_items,
            _depth=_depth + 1,
            _seen=seen,
        )

    return _summarise(value)


def _summarise(value: Any) -> str:
    """A short, safe stand-in for a value we cannot or should not expand."""
    text = repr(value)
    return text if len(text) <= 200 else text[:200] + "…"


def dumps_safe(value: Any, *, indent: int | None = None) -> str:
    """``json.dumps`` that can never fail, whatever is passed in."""
    try:
        return json.dumps(jsonable(value), ensure_ascii=False, indent=indent, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


# ---------------------------------------------------------------------------
# The run's answer
# ---------------------------------------------------------------------------

#: Key an End node adds to its payload when ``include_state`` is on. It is a
#: debugging dump, not the answer, so it is excluded from the candidates.
STATE_DUMP_KEY = "state"

#: Key the manager writes when a run stopped before reaching an End node. See
#: ``RunManager._capture_partial_output``: that payload is a per-node dump, and
#: its shape differs from a real result's.
PARTIAL_KEY = "partial"


def final_output(outputs: Any) -> dict[str, Any] | None:
    """Pick the run's answer out of ``outputs``; ``None`` if there isn't one.

    An End node returns ``{"<output_label>": value}``, so **the name of the
    answer is already a key on the payload** — nothing needs to record it a
    second time. The canvas used to guess with a hardcoded key list
    (``final_ecommerce_solution``, then ``result``), which meant every workflow
    whose ``output_label`` fell outside that list rendered no result at all.

    Returns ``{"key": ..., "value": ...}``. ``None`` means "this payload does not
    unambiguously contain one answer", which is the honest answer for three real
    shapes:

    * a run that stopped before an End node — ``{"partial": True, "vars": {...}}``
    * an ``include_state`` run, once the ``state`` dump is set aside
    * anything with zero or several candidates, where picking one would be a guess

    Reading ``outputs`` instead of the graph definition is deliberate: an
    ``/runs/adhoc`` graph was never saved, a saved graph can be edited after the
    run, and a persisted row has no definition to load. A finished run's
    ``outputs`` never change, so this cannot drift away from what actually ran.

    Tolerant of a non-dict ``outputs`` because this reads persisted rows as well
    as live state, and a row written before some field existed is not a reason to
    raise inside a summary.
    """
    if not isinstance(outputs, dict) or not outputs:
        return None
    # `is True` rather than truthiness: a node's own output_label could be
    # "partial", and a real result under that name is not a partial dump.
    if outputs.get(PARTIAL_KEY) is True:
        return None

    candidates = [key for key in outputs if key != STATE_DUMP_KEY]
    if len(candidates) != 1:
        return None

    key = candidates[0]
    return {"key": key, "value": outputs[key]}


__all__ = [
    "CONTENT_SECRET_PATTERNS",
    "MASK",
    "MAX_SCANNED_STRING",
    "SECRET_HINTS",
    "content_redaction_enabled",
    "dumps_safe",
    "final_output",
    "find_content_secrets",
    "jsonable",
    "looks_secret",
    "redact",
    "redact_content",
]
