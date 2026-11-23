"""Conversation sessions for a deployed agent: messages, storage, and the run.

Why this lives in ``app.runtime`` and uses only the standard library
---------------------------------------------------------------------
An exported agent is a *source copy* of this package (see
``app/runtime/export.py``), so every module listed in ``RUNTIME_PATHS`` has to
import without the platform. In particular it must not reach for ``app.db``:
there is no SQLModel, no SQLAlchemy and no database URL inside an exported
package. ``sqlite3`` from the standard library is the persistence that is always
there, which is why :class:`SQLiteSessionStore` is what a deployed ``main.py``
hands to :class:`SessionService`.

Three separate concerns, deliberately
-------------------------------------
* :class:`Session` / :class:`Message` are plain dataclasses. ``to_dict()`` is
  how they cross the HTTP boundary, so neither FastAPI nor pydantic is needed -
  a deployment with no dependencies at all can still serve a chat.
* :class:`SessionStore` is the persistence seam. :class:`MemorySessionStore` is
  for tests and short-lived processes; :class:`SQLiteSessionStore` survives a
  restart and is the one an exported agent uses.
* :class:`SessionService` owns the chat protocol: it turns a user message into
  the ``inputs`` dict a graph expects, calls ``run_fn``, and records the answer.

The one non-obvious rule
------------------------
History and the current message are **different** keys. The current user message
goes into ``message_key`` (default ``"text"``); the previous turns go into
``history_key`` (default ``"history"``) as ``"user: ...\\nassistant: ...\\n"``
lines. The current message therefore appears in the inputs exactly once, and a
prompt can use both ``{{ text }}`` and ``{{ history }}``. This is compatible
with the Start node: it merges user-supplied inputs over its declared defaults,
so the session layer needs no state-schema change.

Concurrency
-----------
``post_message`` serialises per session with an :class:`asyncio.Lock`, so two
turns of the same conversation can never interleave their history. One lock
belongs to one event loop; a caller that runs each request in a *different*
loop (the standard-library HTTP adapter does, one ``asyncio.run`` per request
thread) must serialise per session itself before calling - ``http_api`` holds a
per-session ``threading.Lock`` for exactly that reason. Different sessions do
not share a lock and can run concurrently.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "FAILED_REPLY_PREFIX",
    "HISTORY_ROLE_LABELS",
    "Message",
    "MemorySessionStore",
    "REPLY_FALLBACK_KEYS",
    "ROLES",
    "RunFn",
    "SQLiteSessionStore",
    "Session",
    "SessionConfig",
    "SessionNotFound",
    "SessionService",
    "SessionStore",
]

#: Roles a message may carry. Kept as a tuple so the error message can list them.
ROLES: tuple[str, ...] = ("user", "assistant", "system")

#: Prefix of the transcript line recorded when ``run_fn`` raises. The failed turn
#: is appended *before* the exception is re-raised: a conversation that silently
#: forgets a question the agent crashed on is misleading, and the caller still
#: gets the exception it has to handle.
FAILED_REPLY_PREFIX = "(运行失败: "

#: Labels used when previous turns are flattened into the ``history`` input.
#: Fixed English, not localised: they are part of the model-facing contract and a
#: workflow prompt may branch on them.
HISTORY_ROLE_LABELS: dict[str, str] = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
}

#: Where the reply text is looked for when ``SessionConfig.output_key`` is unset
#: (or names a key the run did not produce). ``result`` is the End node's default
#: label, so a plain Start -> ... -> End graph is readable without configuration.
REPLY_FALLBACK_KEYS: tuple[str, ...] = (
    "output",
    "answer",
    "reply",
    "text",
    "result",
    "message",
)

#: ``run_fn`` as the export template will provide it: one async call that takes
#: the resolved inputs, optionally reports progress, and returns the run output.
RunFn = Callable[..., Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC ISO-8601 with microseconds, so string ordering is time ordering."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _new_id() -> str:
    """Short unique id, matching the platform's 12-hex-character convention."""
    return uuid.uuid4().hex[:12]


def _loads(raw: Any, default: Any) -> Any:
    """Decode a JSON column, falling back to ``default`` on anything unexpected.

    A hand-edited database or a column written by an older version should cost a
    message its metadata, not crash the whole conversation listing.
    """
    if raw is None or raw == "":
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# The data
# ---------------------------------------------------------------------------


class SessionNotFound(LookupError):
    """No store holds this session id.

    ``LookupError`` for the same reason ``WorkflowNotFound`` is one: an HTTP
    layer can map one exception to 404 without knowing which store answered.
    """


@dataclass
class Message:
    """One turn in a conversation."""

    id: str
    session_id: str
    role: str
    content: str
    created_at: str
    run_id: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(
                f"unknown message role '{self.role}'; expected one of {ROLES}"
            )
        if not isinstance(self.content, str):
            raise TypeError("message content must be a string")
        self.attachments = [dict(item) for item in (self.attachments or [])]
        self.metadata = dict(self.metadata or {})

    def to_prompt_line(self) -> str:
        """One line of the flattened history handed to the graph."""
        label = HISTORY_ROLE_LABELS.get(self.role, self.role)
        return f"{label}: {self.content}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "attachments": [dict(item) for item in self.attachments],
            "metadata": dict(self.metadata),
        }


@dataclass
class Session:
    """A conversation: identity, a human label, and its message counter."""

    id: str
    title: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)
    message_count: int = 0

    def __post_init__(self) -> None:
        self.metadata = dict(self.metadata or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
            "message_count": self.message_count,
        }

# ---------------------------------------------------------------------------
# The store seam
# ---------------------------------------------------------------------------


class SessionStore(ABC):
    """Persistence seam for :class:`Session` and :class:`Message`.

    ``append`` bumps the session's ``message_count`` and ``updated_at`` in the
    same operation, so a caller never has to remember that bookkeeping; the
    session returned by ``get`` is then already consistent.
    """

    @abstractmethod
    def create(
        self, title: str = "", metadata: dict[str, Any] | None = None
    ) -> Session:
        """Create and return a new, empty session."""

    @abstractmethod
    def get(self, session_id: str) -> Session:
        """Return the session, or raise :class:`SessionNotFound`."""

    @abstractmethod
    def list(self, limit: int = 50) -> list[Session]:
        """Sessions, newest first. ``limit`` of None or a negative value = all."""

    @abstractmethod
    def delete(self, session_id: str) -> bool:
        """Delete a session and its messages. ``False`` if it did not exist."""

    @abstractmethod
    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        run_id: str = "",
        attachments: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        """Append one message to the session and return it."""

    @abstractmethod
    def messages(self, session_id: str, limit: int | None = None) -> list[Message]:
        """Messages, oldest first. ``limit`` returns the newest N (still oldest first)."""

    # -- not abstract, but both built-in stores implement it -----------------
    def update(
        self,
        session_id: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """Change the human-facing title and/or metadata of a session.

        Added beyond the original interface sketch because
        ``SessionConfig.title_from_message`` needs a write path - naming a
        conversation after its first question cannot be expressed through
        ``create``. A store that cannot support it may keep this default.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot update a session's title or metadata"
        )


class MemorySessionStore(SessionStore):
    """In-process store: fast, thread-safe, gone when the process exits.

    Used by tests and by a chat deployment that is deliberately ephemeral. The
    ``threading.RLock`` matters because the HTTP adapter serves each request in
    its own thread; the lock is per store, not per session, which is fine for an
    operation measured in microseconds.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._messages: dict[str, list[Message]] = {}
        self._created_order: list[str] = []

    # -- helpers ------------------------------------------------------------
    def _require(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(f"session '{session_id}' not found")
        return session

    # -- interface ----------------------------------------------------------
    def create(
        self, title: str = "", metadata: dict[str, Any] | None = None
    ) -> Session:
        session = Session(
            id=_new_id(), title=title or "", metadata=dict(metadata or {})
        )
        with self._lock:
            self._sessions[session.id] = session
            self._messages[session.id] = []
            self._created_order.append(session.id)
            return replace(session)

    def get(self, session_id: str) -> Session:
        with self._lock:
            return replace(self._require(session_id))

    def list(self, limit: int = 50) -> list[Session]:
        with self._lock:
            ids = list(reversed(self._created_order))
            if limit is not None and limit >= 0:
                ids = ids[:limit]
            return [replace(self._sessions[i]) for i in ids if i in self._sessions]

    def delete(self, session_id: str) -> bool:
        with self._lock:
            if self._sessions.pop(session_id, None) is None:
                return False
            self._messages.pop(session_id, None)
            try:
                self._created_order.remove(session_id)
            except ValueError:  # pragma: no cover - order list is maintained here
                pass
            return True

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        run_id: str = "",
        attachments: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        with self._lock:
            session = self._require(session_id)
            now = _now_iso()
            message = Message(
                id=_new_id(),
                session_id=session_id,
                role=role,
                content=content,
                created_at=now,
                run_id=run_id or "",
                attachments=attachments,
                metadata=metadata,
            )
            self._messages[session_id].append(message)
            session.message_count += 1
            session.updated_at = now
            return replace(message)

    def messages(self, session_id: str, limit: int | None = None) -> list[Message]:
        with self._lock:
            self._require(session_id)
            items = list(self._messages.get(session_id, []))
        if limit is not None and limit >= 0:
            items = items[-limit:] if limit else []
        return [replace(message) for message in items]

    def update(
        self,
        session_id: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        with self._lock:
            session = self._require(session_id)
            if title is not None:
                session.title = title
            if metadata is not None:
                session.metadata = dict(metadata)
            session.updated_at = _now_iso()
            return replace(session)

#: Schema for :class:`SQLiteSessionStore`. ``CREATE ... IF NOT EXISTS`` makes
#: opening an existing database a no-op, so the store has no migration story to
#: get wrong: idempotent creation is the whole story.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    message_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, created_at);
"""


class SQLiteSessionStore(SessionStore):
    """A store that survives a restart, using only :mod:`sqlite3`.

    Thread safety is one long-lived connection guarded by a
    :class:`threading.RLock`, not a connection per operation. A single writer is
    what SQLite wants, the lock makes that explicit, and a persistent connection
    is what makes ``":memory:"`` work too (a per-operation connection to
    ``:memory:`` would, by definition, always be empty).

    WAL is enabled on a file database so the HTTP thread that writes a message
    does not block another thread listing sessions, and ``busy_timeout`` is set
    so a second process holding the file produces a bounded wait instead of an
    immediate ``database is locked``. The journal mode is a property of the
    database file, so ``close()`` and reopen does not lose it.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._closed = False
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path), timeout=5.0, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        with self._lock:
            self._connection.executescript(_SCHEMA_SQL)
            self._connection.commit()

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        """Close the connection. Idempotent; the store is unusable afterwards."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def __enter__(self) -> "SQLiteSessionStore":
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            title=row["title"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=_loads(row["metadata"], {}),
            message_count=int(row["message_count"] or 0),
        )

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        return Message(
            id=row["id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            created_at=row["created_at"],
            run_id=row["run_id"] or "",
            attachments=_loads(row["attachments"], []),
            metadata=_loads(row["metadata"], {}),
        )

    def _require(self, session_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise SessionNotFound(f"session '{session_id}' not found")
        return row

    @staticmethod
    def _limit_clause(limit: int | None) -> int:
        """SQLite spells "no limit" as a negative LIMIT."""
        if limit is None or limit < 0:
            return -1
        return int(limit)

    # -- interface ----------------------------------------------------------
    def create(
        self, title: str = "", metadata: dict[str, Any] | None = None
    ) -> Session:
        session = Session(
            id=_new_id(), title=title or "", metadata=dict(metadata or {})
        )
        with self._lock:
            self._connection.execute(
                "INSERT INTO sessions "
                "(id, title, created_at, updated_at, metadata, message_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    session.title,
                    session.created_at,
                    session.updated_at,
                    _dumps(session.metadata),
                    session.message_count,
                ),
            )
            self._connection.commit()
        return session

    def get(self, session_id: str) -> Session:
        with self._lock:
            return self._row_to_session(self._require(session_id))

    def list(self, limit: int = 50) -> list[Session]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC, rowid DESC "
                "LIMIT ?",
                (self._limit_clause(limit),),
            ).fetchall()
            return [self._row_to_session(row) for row in rows]

    def delete(self, session_id: str) -> bool:
        with self._lock:
            try:
                self._connection.execute(
                    "DELETE FROM messages WHERE session_id = ?", (session_id,)
                )
                cursor = self._connection.execute(
                    "DELETE FROM sessions WHERE id = ?", (session_id,)
                )
                self._connection.commit()
            except sqlite3.Error:
                self._connection.rollback()
                raise
            return cursor.rowcount > 0

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        run_id: str = "",
        attachments: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        now = _now_iso()
        message = Message(
            id=_new_id(),
            session_id=session_id,
            role=role,
            content=content,
            created_at=now,
            run_id=run_id or "",
            attachments=attachments,
            metadata=metadata,
        )
        with self._lock:
            self._require(session_id)
            try:
                self._connection.execute(
                    "INSERT INTO messages "
                    "(id, session_id, role, content, created_at, run_id, "
                    "attachments, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        message.id,
                        message.session_id,
                        message.role,
                        message.content,
                        message.created_at,
                        message.run_id,
                        _dumps(message.attachments),
                        _dumps(message.metadata),
                    ),
                )
                # The bookkeeping is part of the same transaction as the insert:
                # a message visible without its session's count is a bug that
                # only shows up after a crash, which is the worst time to find it.
                self._connection.execute(
                    "UPDATE sessions SET message_count = message_count + 1, "
                    "updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                self._connection.commit()
            except sqlite3.Error:
                self._connection.rollback()
                raise
        return message

    def messages(self, session_id: str, limit: int | None = None) -> list[Message]:
        with self._lock:
            self._require(session_id)
            if limit is not None and limit >= 0:
                # Newest N, then reversed to the oldest-first contract.
                rows = self._connection.execute(
                    "SELECT * FROM messages WHERE session_id = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (session_id, self._limit_clause(limit)),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = self._connection.execute(
                    "SELECT * FROM messages WHERE session_id = ? "
                    "ORDER BY created_at ASC, rowid ASC",
                    (session_id,),
                ).fetchall()
            return [self._row_to_message(row) for row in rows]

    def update(
        self,
        session_id: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        with self._lock:
            row = self._require(session_id)
            new_title = row["title"] if title is None else title
            new_metadata = (
                _loads(row["metadata"], {}) if metadata is None else dict(metadata)
            )
            now = _now_iso()
            self._connection.execute(
                "UPDATE sessions SET title = ?, metadata = ?, updated_at = ? "
                "WHERE id = ?",
                (new_title, _dumps(new_metadata), now, session_id),
            )
            self._connection.commit()
            return Session(
                id=row["id"],
                title=new_title or "",
                created_at=row["created_at"],
                updated_at=now,
                metadata=new_metadata,
                message_count=int(row["message_count"] or 0),
            )


# ---------------------------------------------------------------------------
# The chat protocol
# ---------------------------------------------------------------------------


@dataclass
class SessionConfig:
    """How a session's messages become a graph's inputs and back."""

    #: Input key that carries the current user message.
    message_key: str = "text"
    #: Input key that carries the previous turns as text. Empty disables it.
    history_key: str = "history"
    #: Output key that carries the reply. Empty means "guess from the fallbacks".
    output_key: str = ""
    #: How many previous messages to flatten into ``history``.
    history_limit: int = 20
    #: Truncate a user message to this many characters (0 disables truncation).
    max_message_chars: int = 8000
    #: Inputs always supplied, before the message and history keys are set.
    default_inputs: dict[str, Any] = field(default_factory=dict)
    #: Name a new conversation after its first message, up to this many chars
    #: (0 disables auto-titling; an explicit title is never overwritten).
    title_from_message: int = 40


def _format_history(messages: Iterable[Message]) -> str:
    """Flatten previous turns into the ``history`` input.

    One ``"role: content"`` line per message plus a trailing newline, which is
    the shape a prompt template can paste into an instruction block. Empty input
    yields an empty string rather than no key: the Start node always supplies the
    declared defaults, so a template may reference ``{{ history }}`` on the very
    first turn without hitting an undefined variable.
    """
    return "".join(f"{message.to_prompt_line()}\n" for message in messages)


def _coerce_reply(value: Any) -> str | None:
    """A string if ``value`` is text-like enough to be the reply, else None."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        only = next(iter(value.values()))
        if isinstance(only, str):
            return only
    return None


def _reply_text(output: Any, output_key: str = "") -> str:
    """Extract the assistant's answer from whatever the graph returned.

    The order is deliberately narrow-to-broad: the configured key, then the
    conventional keys, then "the only string in the object", then the JSON form.
    A graph that returns ``{"result": "hi"}`` (the End node's default shape) is
    read without configuration; a graph that returns a rich object keeps it as
    JSON instead of silently losing fields.
    """
    if isinstance(output, str):
        return output
    if not isinstance(output, dict):
        return _dumps(output)
    if output_key and output_key in output:
        text = _coerce_reply(output[output_key])
        if text is not None:
            return text
    for key in REPLY_FALLBACK_KEYS:
        if key in output:
            text = _coerce_reply(output[key])
            if text is not None:
                return text
    non_empty = [
        value for value in output.values() if isinstance(value, str) and value.strip()
    ]
    if len(non_empty) == 1:
        return non_empty[0]
    strings = [value for value in output.values() if isinstance(value, str)]
    if len(strings) == 1:
        return strings[0]
    return _dumps(output)


def _title_from_message(text: str, limit: int) -> str:
    """A one-line title taken from the first user message."""
    single_line = " ".join(text.split())
    if limit <= 0:
        return single_line
    return single_line[:limit]


class SessionService:
    """The chat protocol on top of a store and an async ``run_fn``.

    ``run_fn`` is the exported agent's execution entry point: it receives the
    resolved ``inputs`` dict and an optional ``emit`` callback, and returns the
    run output as a dict. The same object serves ``POST /v1/invoke`` (no store,
    no history) and ``POST /v1/sessions/{id}/messages`` (this class).
    """

    def __init__(
        self,
        store: SessionStore,
        run_fn: RunFn,
        config: SessionConfig | None = None,
    ) -> None:
        self.store = store
        self.run_fn = run_fn
        self.config = config or SessionConfig()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- session helpers ----------------------------------------------------
    def create_session(
        self, title: str = "", metadata: dict[str, Any] | None = None
    ) -> Session:
        return self.store.create(title=title, metadata=metadata)

    def get_session(self, session_id: str) -> Session:
        return self.store.get(session_id)

    def list_sessions(self, limit: int = 50) -> list[Session]:
        return self.store.list(limit=limit)

    def session_messages(
        self, session_id: str, limit: int | None = None
    ) -> list[Message]:
        return self.store.messages(session_id, limit=limit)

    def delete_session(self, session_id: str) -> bool:
        deleted = self.store.delete(session_id)
        if deleted:
            with self._locks_guard:
                self._locks.pop(session_id, None)
        return deleted

    # -- the turn -----------------------------------------------------------
    async def post_message(
        self,
        session_id: str,
        text: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Run one turn of a conversation.

        Returns ``{"session": Session, "user": Message, "assistant": Message,
        "output": dict}``. A failed run records an assistant error message and
        re-raises; a successful run never mutates the return value of ``run_fn``
        (the ``"__run_id__"`` hint is read from a copy).
        """
        config = self.config
        message_text = self._clean_text(text)
        attachment_list = self._clean_attachments(attachments)

        lock = self._lock_for(session_id)
        async with lock:
            session = self.store.get(session_id)
            # Previous turns only: the current message is injected as
            # ``message_key``, so it is in the prompt exactly once.
            history = (
                self.store.messages(session_id, limit=config.history_limit)
                if config.history_key
                else []
            )
            user_message = self.store.append(
                session_id,
                "user",
                message_text,
                attachments=attachment_list,
            )
            if not session.title and config.title_from_message > 0:
                title = _title_from_message(message_text, config.title_from_message)
                if title:
                    session = self.store.update(session_id, title=title)

            inputs = self._build_inputs(message_text, history)
            try:
                output = await self._invoke(inputs, emit)
            except Exception as exc:
                # Record the failed turn before re-raising: the caller (HTTP or a
                # test) still gets the exception, but the transcript shows what
                # was asked and that it failed.
                detail = str(exc) or type(exc).__name__
                self.store.append(
                    session_id,
                    "assistant",
                    f"{FAILED_REPLY_PREFIX}{detail})",
                    metadata={"error": type(exc).__name__},
                )
                raise

            reply_text, run_id, payload = self._reply_from_output(output)
            assistant_message = self.store.append(
                session_id, "assistant", reply_text, run_id=run_id
            )
            return {
                "session": self.store.get(session_id),
                "user": user_message,
                "assistant": assistant_message,
                "output": payload,
            }

    # -- internals ----------------------------------------------------------
    def _lock_for(self, session_id: str) -> asyncio.Lock:
        """The per-session lock.

        Creating it under a thread lock keeps two HTTP threads from racing to
        create two different locks for one session.
        """
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    async def _invoke(
        self,
        inputs: dict[str, Any],
        emit: Callable[[dict[str, Any]], None] | None,
    ) -> Any:
        """Call ``run_fn`` with progress reporting only when it was asked for.

        ``run_fn(inputs, emit=None)`` is the contract; passing the callback
        positionally only when a caller supplied one lets a plain
        ``run_once(inputs)`` be wired for the non-streaming endpoints without a
        wrapper, while a streaming caller gets progress events.
        """
        if emit is None:
            return await self.run_fn(inputs)
        return await self.run_fn(inputs, emit)

    def _build_inputs(self, text: str, history: list[Message]) -> dict[str, Any]:
        config = self.config
        inputs: dict[str, Any] = dict(config.default_inputs or {})
        if config.message_key:
            inputs[config.message_key] = text
        if config.history_key:
            inputs[config.history_key] = _format_history(history)
        return inputs

    def _clean_text(self, text: Any) -> str:
        if not isinstance(text, str):
            raise ValueError("message text must be a string")
        if not text.strip():
            raise ValueError("message text is empty")
        limit = self.config.max_message_chars
        if limit and len(text) > limit:
            return text[:limit]
        return text

    @staticmethod
    def _clean_attachments(
        attachments: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if attachments is None:
            return []
        if not isinstance(attachments, (list, tuple)):
            raise ValueError("attachments must be a list of objects")
        cleaned: list[dict[str, Any]] = []
        for item in attachments:
            if not isinstance(item, dict):
                raise ValueError("each attachment must be an object")
            cleaned.append(dict(item))
        return cleaned

    def _reply_from_output(
        self, output: Any
    ) -> tuple[str, str, dict[str, Any]]:
        """Split a run's output into ``(reply text, run id, public payload)``."""
        if output is None:
            payload: dict[str, Any] = {}
        elif isinstance(output, dict):
            # Copy: the caller's dict must not lose its private hint key, and the
            # value returned to the client must not contain it.
            payload = dict(output)
        else:
            payload = {"output": output}
        run_id = ""
        hint = payload.pop("__run_id__", "")
        if hint:
            run_id = str(hint)
        return _reply_text(payload, self.config.output_key), run_id, payload
