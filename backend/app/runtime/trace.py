"""Trace ids, in the W3C `traceparent` format.

Why the W3C format and not a private one
----------------------------------------
The field already existed — every request needs *some* id to correlate its log
lines — and the only question was what shape. `secrets.token_hex(16)` would have
worked and would have been a private convention that no tracing tool can read.

This uses `traceparent` instead, because it is a W3C Recommendation (23 Nov 2021)
with a fixed, parseable layout:

    traceparent: 00-<trace-id>-<parent-id>-<flags>
                   ^version  ^32 hex     ^16 hex    ^2 hex

so a deployment that later adds OpenTelemetry, a proxy, or any vendor's collector
can correlate these traces without this code changing. It costs nothing: the same
`secrets` call, formatted.

What is deliberately *not* here
-------------------------------
**No OpenTelemetry SDK.** Task 25's `capabilities.yaml` sketch offers
`tracer: otel | logs`, and `logs` is the default. This is the `logs` half: an id
that correlates log lines and API responses. An OTel exporter is a large
dependency and a configuration surface of its own; adding it now would be
building the second option before the first is used.

**No propagation to other processes yet.** The id is context-local and reaches
log records and responses. Sending it to an outbound HTTP call or a scheduled run
is a real gap, recorded rather than hidden.

Rejecting an incoming header
----------------------------
The spec says a malformed `traceparent` MUST be ignored and the trace restarted,
and that is what :func:`parse_traceparent` does — returns ``None`` rather than
raising. A caller that trusted a bad header would either blow up on it or, worse,
adopt an id an attacker chose and correlate unrelated requests under it.
"""

from __future__ import annotations

import contextvars
import logging
import re
import secrets

#: The header and the attribute name, so the wire format and the log field cannot
#: drift apart. `logging` supports extra={"trace_id": ...} and a formatter reads
#: it by this key.
HEADER = "traceparent"
FIELD = "trace_id"

#: `00-<32 lowercase hex>-<16 lowercase hex>-<2 lowercase hex>`.
#:
#: Anchored and exact-length. A loose pattern would accept a truncated id, and
#: then two different requests could share one.
_PATTERN = re.compile(
    r"^00-(?P<trace>[0-9a-f]{32})-(?P<parent>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)

#: All-zero is explicitly invalid for both ids. Checked separately from the
#: pattern because `0`*32 matches `[0-9a-f]{32}` and would otherwise pass as a
#: legitimate id that every request shares.
_ZEROES = frozenset({"0"})

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)


def new_trace_id() -> str:
    """A fresh 32-hex trace id."""
    return secrets.token_hex(16)


def new_parent_id() -> str:
    """A fresh 16-hex span id, as the `parent-id` field."""
    return secrets.token_hex(8)


def format_traceparent(trace_id: str, parent_id: str | None = None, sampled: bool = False) -> str:
    """Build the header value. `sampled` sets the low bit of the flags field."""
    return f"00-{trace_id}-{parent_id or new_parent_id()}-{'01' if sampled else '00'}"


def parse_traceparent(value: str | None) -> str | None:
    """The trace id from an incoming header, or ``None`` if it is not usable.

    ``None`` for missing, malformed, wrong-version, or all-zero — every case the
    spec says to restart the trace. Callers do not need to distinguish them,
    because the response is the same in all four: make a new id.
    """
    if not value:
        return None
    match = _PATTERN.match(value.strip())
    if match is None:
        return None
    trace_id = match.group("trace")
    parent = match.group("parent")
    if set(trace_id) == _ZEROES or set(parent) == _ZEROES:
        return None
    return trace_id


def get_trace_id() -> str | None:
    """The id for the work happening now, or ``None`` outside a request."""
    return _current.get()


def set_trace_id(trace_id: str | None) -> contextvars.Token:
    """Set the id for this context, returning the token that restores it.

    `contextvars` rather than a thread-local or a global: a run executes as an
    asyncio task, and several runs share a thread. A global would let one run's
    id overwrite another's, which is the failure this exists to prevent.
    """
    return _current.set(trace_id)


def reset_trace_id(token: contextvars.Token) -> None:
    """Undo a :func:`set_trace_id`. Always call it, in a `finally`."""
    _current.reset(token)


class TraceIdFilter(logging.Filter):
    """Put the current trace id on every log record.

    A `logging.Filter` rather than a wrapper around every `logger.info(...)`
    call: there are 26 of those across 10 modules, and a wrapper would have to be
    remembered at each new one. A filter is installed once on the handler and
    every record gets the field whether its author thought about it or not.

    Absent outside a request, and the formatter prints a placeholder rather than
    dropping the column, so log lines stay aligned.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or "-"
        return True
