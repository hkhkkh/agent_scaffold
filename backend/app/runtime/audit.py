"""Append-only audit trail for capability actions.

Why this is not a log line
--------------------------
A log is for a person debugging today; an audit entry answers "what did this
agent actually do" after the fact -- possibly for a customer, possibly months
later. They differ in retention and in who is allowed to turn them off, so
mixing them means the audit answer silently degrades the first time someone
lowers a log level.

JSON Lines on purpose: appending is one write, tailing is trivial, and no
database is needed on a customer's machine. The file is rotated by size so it
cannot fill a disk on its own.

Failure policy
--------------
:class:`JsonlAuditRecorder` raises when it cannot write. The tools that mutate
state record *before* acting (and, where it matters, act inside the recorded
intent), so "the disk is full" fails the write rather than allowing an
unrecorded one. That is the whole reason the API has no silent fallback.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from app.settings import get_settings

__all__ = [
    "AuditError",
    "AuditEvent",
    "AuditRecorder",
    "JsonlAuditRecorder",
    "NullAuditRecorder",
    "build_audit_recorder",
    "get_audit_recorder",
    "record_audit",
    "set_audit_recorder",
]

#: Rotate the audit file at this size. Same order as the daemon's own log.
DEFAULT_MAX_BYTES = 10_000_000


class AuditError(RuntimeError):
    """The audit entry could not be written, so the action must not proceed."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass(frozen=True)
class AuditEvent:
    """One immutable thing that happened.

    ``action`` is a stable verb (`file.write`, `http.request`); ``outcome`` is
    `ok` / `denied` / `error`; ``detail`` stays JSON-serialisable and small --
    an audit trail that holds entire file contents is a liability of its own.
    """

    action: str
    outcome: str = "ok"
    actor: str = "agent"
    tool: str = ""
    target: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at or _utc_now(),
            "action": self.action,
            "outcome": self.outcome,
            "actor": self.actor,
            "tool": self.tool,
            "target": self.target,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AuditEvent":
        return cls(
            action=str(payload.get("action", "")),
            outcome=str(payload.get("outcome", "ok")),
            actor=str(payload.get("actor", "agent")),
            tool=str(payload.get("tool", "")),
            target=str(payload.get("target", "")),
            detail=dict(payload.get("detail") or {}),
            at=str(payload.get("at", "")),
        )


class AuditRecorder:
    """Where audit events go. One implementation per deployment."""

    def record(self, event: AuditEvent) -> None:
        raise NotImplementedError


class NullAuditRecorder(AuditRecorder):
    """Drop everything. For tests and for a deployment that keeps its own copy."""

    def record(self, event: AuditEvent) -> None:  # noqa: D102 - deliberately inert
        return None


class JsonlAuditRecorder(AuditRecorder):
    """Append JSON lines under a lock, rotating by size."""

    def __init__(self, path: Path | str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def record(self, event: AuditEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
            except OSError as exc:
                raise AuditError(f"cannot write audit log {self.path}: {exc}") from exc


def build_audit_recorder(settings: Any = None) -> AuditRecorder:
    """Construct the recorder from configuration.

    * ``AUDIT_LOG`` = ``none``/``null``/empty -> :class:`NullAuditRecorder`
    * ``AUDIT_LOG`` = a path -> JSON Lines at that path
    * otherwise -> ``<data_dir>/audit.jsonl`` (the same place runs/logs live)
    """
    settings = settings or get_settings()
    configured = (os.environ.get("AUDIT_LOG") or "").strip()
    if configured:
        if configured.lower() in ("none", "null", "off", "0", "false"):
            return NullAuditRecorder()
        return JsonlAuditRecorder(configured)
    return JsonlAuditRecorder(Path(settings.data_dir) / "audit.jsonl")


#: Process-wide recorder. Swapped in tests; the seam an exported agent sets up
#: from its own data directory.
_recorder: AuditRecorder | None = None


def get_audit_recorder() -> AuditRecorder:
    global _recorder
    if _recorder is None:
        _recorder = build_audit_recorder()
    return _recorder


def set_audit_recorder(recorder: AuditRecorder | None) -> None:
    """Override the process recorder. ``None`` restores the configured one."""
    global _recorder
    _recorder = recorder


def record_audit(
    action: str,
    *,
    outcome: str = "ok",
    actor: str = "agent",
    tool: str = "",
    target: str = "",
    detail: Mapping[str, Any] | None = None,
) -> None:
    """Record one event with the process recorder.

    Raises :class:`AuditError` when the recorder cannot write; callers that mutate
    state rely on that to fail closed.
    """
    get_audit_recorder().record(
        AuditEvent(
            action=action,
            outcome=outcome,
            actor=actor,
            tool=tool,
            target=target,
            detail=dict(detail or {}),
            at=_utc_now(),
        )
    )
