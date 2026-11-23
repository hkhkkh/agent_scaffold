"""Sessions and the chat protocol: storage, inputs, and concurrency.

The failure modes this pins, in the order they break a chat quietly:

* a store whose list order is wrong, or whose ``messages`` returns the oldest N
  when a chat needs the newest N for the prompt;
* inputs that carry the current user message twice (once as its own input and
  once inside ``history``) - the model answers the same question against itself;
* two turns of one conversation interleaving their history because the store
  was read before the other turn appended its answer;
* a crashed run that loses the user's question from the transcript;
* a title that names every conversation the same way, or overwrites the one a
  user set.

The two stores are tested through one parametrised fixture so a behaviour cannot
be true of memory and false of the store that survives a restart.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from app.runtime.session import (
    FAILED_REPLY_PREFIX,
    MemorySessionStore,
    SessionConfig,
    SessionNotFound,
    SessionService,
    SQLiteSessionStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    """Both implementations, same contract."""
    if request.param == "memory":
        yield MemorySessionStore()
        return
    with SQLiteSessionStore(tmp_path / "sessions.db") as sqlite_store:
        yield sqlite_store


class _Recorder:
    """A ``run_fn`` that records its inputs and returns a fixed output."""

    def __init__(self, output=None, reply="ok"):
        self.output = {"result": reply} if output is None else output
        self.inputs: list[dict] = []

    async def __call__(self, inputs, emit=None):
        self.inputs.append(dict(inputs))
        return json.loads(json.dumps(self.output))


def _service(store, run_fn, **config):
    return SessionService(store, run_fn, SessionConfig(**config))


def _assert_iso(value: str) -> None:
    assert isinstance(value, str) and value
    # Parseable ISO-8601, and timezone-aware: sorting these strings is sorting
    # time, which is what the SQLite ORDER BY relies on.
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# The store contract
# ---------------------------------------------------------------------------


class TestTheStoreContract:
    def test_create_and_get_roundtrip(self, store):
        session = store.create("Budget review", {"owner": "ops"})
        fetched = store.get(session.id)

        assert fetched.id == session.id
        assert fetched.title == "Budget review"
        assert fetched.metadata == {"owner": "ops"}
        assert fetched.message_count == 0
        _assert_iso(fetched.created_at)
        _assert_iso(fetched.updated_at)

    def test_a_default_session_has_no_title_but_a_valid_id(self, store):
        session = store.create()
        assert session.title == ""
        assert session.id
        assert store.get(session.id).id == session.id

    def test_get_an_unknown_session_raises_lookup_error(self, store):
        with pytest.raises(SessionNotFound):
            store.get("does-not-exist")

    def test_list_is_newest_first_and_honours_the_limit(self, store):
        first = store.create("one")
        second = store.create("two")
        third = store.create("three")

        assert [s.id for s in store.list()] == [third.id, second.id, first.id]
        assert [s.id for s in store.list(limit=2)] == [third.id, second.id]
        assert store.list(limit=0) == []

    def test_delete_removes_the_session_and_its_messages(self, store):
        session = store.create("doomed")
        store.append(session.id, "user", "hello")

        assert store.delete(session.id) is True
        with pytest.raises(SessionNotFound):
            store.get(session.id)
        with pytest.raises(SessionNotFound):
            store.messages(session.id)

    def test_delete_an_unknown_session_is_false_not_an_error(self, store):
        assert store.delete("never-existed") is False

    def test_append_bumps_the_counter_and_the_update_time(self, store):
        session = store.create()
        message = store.append(session.id, "user", "hello")

        updated = store.get(session.id)
        assert updated.message_count == 1
        assert updated.updated_at >= session.updated_at
        assert message.session_id == session.id
        assert message.role == "user"
        assert message.content == "hello"

    def test_messages_are_oldest_first(self, store):
        session = store.create()
        store.append(session.id, "user", "u1")
        store.append(session.id, "assistant", "a1")
        store.append(session.id, "user", "u2")

        assert [m.content for m in store.messages(session.id)] == ["u1", "a1", "u2"]

    def test_messages_with_a_limit_return_the_newest_n_still_oldest_first(self, store):
        session = store.create()
        for index in range(5):
            store.append(session.id, "user", f"m{index}")

        assert [m.content for m in store.messages(session.id, limit=2)] == [
            "m3",
            "m4",
        ]
        assert store.messages(session.id, limit=0) == []

    def test_messages_of_an_unknown_session_raise(self, store):
        with pytest.raises(SessionNotFound):
            store.messages("nope")

    def test_append_to_an_unknown_session_raises(self, store):
        with pytest.raises(SessionNotFound):
            store.append("nope", "user", "hello")

    def test_an_unknown_role_is_refused(self, store):
        session = store.create()
        with pytest.raises(ValueError):
            store.append(session.id, "tool", "hello")

    def test_attachments_and_metadata_survive_the_roundtrip(self, store):
        session = store.create()
        store.append(
            session.id,
            "user",
            "see attached",
            run_id="run-1",
            attachments=[{"name": "rates.xlsx", "size": 42}],
            metadata={"source": "chat"},
        )

        message = store.messages(session.id)[0]
        assert message.attachments == [{"name": "rates.xlsx", "size": 42}]
        assert message.metadata == {"source": "chat"}
        assert message.run_id == "run-1"

    def test_update_changes_the_title_and_keeps_the_metadata(self, store):
        session = store.create("", {"owner": "ops"})
        updated = store.update(session.id, title="Named later")

        assert updated.title == "Named later"
        assert updated.metadata == {"owner": "ops"}
        assert store.get(session.id).title == "Named later"

    def test_to_dict_is_plain_json_data(self, store):
        session = store.create("title", {"n": 1})
        message = store.append(session.id, "user", "hi")

        assert json.loads(json.dumps(session.to_dict()))["title"] == "title"
        assert json.loads(json.dumps(message.to_dict()))["content"] == "hi"


# ---------------------------------------------------------------------------
# SQLite specifics: the store that survives a restart
# ---------------------------------------------------------------------------


class TestTheSQLiteStore:
    def test_data_survives_close_and_reopen(self, tmp_path):
        path = tmp_path / "sessions.db"
        with SQLiteSessionStore(path) as first:
            session = first.create("Persisted", {"owner": "ops"})
            first.append(session.id, "user", "before restart")
            first.append(session.id, "assistant", "answer")
            session_id = session.id

        with SQLiteSessionStore(path) as second:
            reopened = second.get(session_id)
            assert reopened.title == "Persisted"
            assert reopened.metadata == {"owner": "ops"}
            assert reopened.message_count == 2
            assert [m.content for m in second.messages(session_id)] == [
                "before restart",
                "answer",
            ]

    def test_it_is_a_wal_database_with_a_busy_timeout(self, tmp_path):
        with SQLiteSessionStore(tmp_path / "sessions.db") as sqlite_store:
            journal_mode = sqlite_store._connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
            busy_timeout = sqlite_store._connection.execute(
                "PRAGMA busy_timeout"
            ).fetchone()[0]

        assert journal_mode.lower() == "wal"
        assert busy_timeout == 5000

    def test_an_in_memory_database_works_too(self):
        with SQLiteSessionStore(":memory:") as sqlite_store:
            session = sqlite_store.create("ephemeral")
            sqlite_store.append(session.id, "user", "hi")
            assert [m.content for m in sqlite_store.messages(session.id)] == ["hi"]


# ---------------------------------------------------------------------------
# What a turn does to the graph's inputs
# ---------------------------------------------------------------------------


class TestTheInputsContract:
    async def test_the_first_turn_has_the_message_and_an_empty_history(self):
        recorder = _Recorder()
        service = _service(MemorySessionStore(), recorder)
        session = service.create_session()

        await service.post_message(session.id, "hello")

        assert recorder.inputs == [{"text": "hello", "history": ""}]

    async def test_the_second_turn_history_holds_the_first_exchange(self):
        recorder = _Recorder()
        service = _service(MemorySessionStore(), recorder)
        session = service.create_session()

        await service.post_message(session.id, "hello")
        await service.post_message(session.id, "again")

        first, second = recorder.inputs
        assert first == {"text": "hello", "history": ""}
        assert second["text"] == "again"
        assert second["history"] == "user: hello\nassistant: ok\n"
        # The current message is in the prompt exactly once, never in history.
        assert "again" not in second["history"]

    async def test_custom_keys_and_default_inputs_are_merged(self):
        recorder = _Recorder()
        service = _service(
            MemorySessionStore(),
            recorder,
            message_key="query",
            history_key="conversation",
            default_inputs={"language": "zh", "query": "a default to be overridden"},
        )
        session = service.create_session()

        await service.post_message(session.id, "real question")

        assert recorder.inputs[0] == {
            "language": "zh",
            "query": "real question",
            "conversation": "",
        }

    async def test_an_empty_history_key_is_not_injected(self):
        recorder = _Recorder()
        service = _service(MemorySessionStore(), recorder, history_key="")
        session = service.create_session()

        await service.post_message(session.id, "hello")

        assert recorder.inputs[0] == {"text": "hello"}

    async def test_history_limit_keeps_only_the_newest_messages(self):
        recorder = _Recorder(reply="answer")
        service = _service(MemorySessionStore(), recorder, history_limit=2)
        session = service.create_session()

        for question in ("q1", "q2", "q3"):
            await service.post_message(session.id, question)

        # Before q3 the transcript is u:q1 a:answer u:q2 a:answer; a limit of 2
        # keeps the newest two, still in chronological order.
        assert recorder.inputs[2]["history"] == (
            "user: q2\nassistant: answer\n"
        )

    async def test_a_message_longer_than_the_limit_is_truncated(self):
        recorder = _Recorder()
        service = _service(MemorySessionStore(), recorder, max_message_chars=5)
        session = service.create_session()

        result = await service.post_message(session.id, "abcdefghij")

        assert result["user"].content == "abcde"
        assert recorder.inputs[0]["text"] == "abcde"

    @pytest.mark.parametrize("text", ["", "   \n\t ", None, 42, ["hi"]])
    async def test_an_empty_or_non_string_message_is_refused(self, text):
        recorder = _Recorder()
        service = _service(MemorySessionStore(), recorder)
        session = service.create_session()

        with pytest.raises(ValueError):
            await service.post_message(session.id, text)

        assert service.session_messages(session.id) == []
        assert recorder.inputs == []

    async def test_an_unknown_session_is_reported_before_the_run(self):
        recorder = _Recorder()
        service = _service(MemorySessionStore(), recorder)

        with pytest.raises(SessionNotFound):
            await service.post_message("never-existed", "hello")

        assert recorder.inputs == []


# ---------------------------------------------------------------------------
# What a turn records: the reply, the run id, the failure, the attachments
# ---------------------------------------------------------------------------


class TestTheReply:
    async def test_the_output_key_wins_over_the_fallbacks(self):
        recorder = _Recorder(output={"final": "chosen", "result": "ignored"})
        service = _service(MemorySessionStore(), recorder, output_key="final")
        session = service.create_session()

        result = await service.post_message(session.id, "hi")

        assert result["assistant"].content == "chosen"

    @pytest.mark.parametrize(
        "output, expected",
        [
            ({"output": "x"}, "x"),
            ({"answer": "x"}, "x"),
            ({"reply": "x"}, "x"),
            ({"text": "x"}, "x"),
            ({"result": "x"}, "x"),
            ({"message": "x"}, "x"),
            ({"only": "x"}, "x"),  # the only string value in the object
            ({"a": 1, "b": "x", "c": 2}, "x"),
            ({"output": {"result": "x"}}, "x"),  # one-key dict wrapped around text
            ({"x": {"deep": True}}, json.dumps({"x": {"deep": True}}, ensure_ascii=False)),
            ({"a": "x", "b": "y"}, json.dumps({"a": "x", "b": "y"}, ensure_ascii=False)),
        ],
    )
    async def test_the_fallback_chain(self, output, expected):
        service = SessionService(MemorySessionStore(), _Recorder(output=output))
        session = service.create_session()

        result = await service.post_message(session.id, "hi")

        assert result["assistant"].content == expected

    async def test_the_run_id_hint_moves_from_the_output_to_the_message(self):
        shared = {"result": "ok", "__run_id__": "run-9"}

        async def run_fn(inputs, emit=None):
            return shared

        service = SessionService(MemorySessionStore(), run_fn)
        session = service.create_session()

        result = await service.post_message(session.id, "hi")

        assert result["assistant"].run_id == "run-9"
        assert result["output"] == {"result": "ok"}
        # The caller's dict is not mutilated: the hint is read from a copy.
        assert shared == {"result": "ok", "__run_id__": "run-9"}
        assert service.session_messages(session.id)[1].run_id == "run-9"

    async def test_per_run_metadata_is_not_needed_to_read_a_plain_string(self):
        async def run_fn(inputs, emit=None):
            return "just a string"

        service = SessionService(MemorySessionStore(), run_fn)
        session = service.create_session()

        result = await service.post_message(session.id, "hi")

        assert result["assistant"].content == "just a string"

    async def test_attachments_are_recorded_on_the_user_message(self):
        recorder = _Recorder()
        service = _service(MemorySessionStore(), recorder)
        session = service.create_session()

        result = await service.post_message(
            session.id, "see attached", attachments=[{"name": "rates.xlsx"}]
        )

        assert result["user"].attachments == [{"name": "rates.xlsx"}]
        assert service.session_messages(session.id)[0].attachments == [
            {"name": "rates.xlsx"}
        ]


class TestAFailedRun:
    async def test_the_error_turn_is_recorded_then_the_error_is_re_raised(self):
        async def boom(inputs, emit=None):
            raise RuntimeError("model is down")

        service = SessionService(MemorySessionStore(), boom)
        session = service.create_session()

        with pytest.raises(RuntimeError, match="model is down"):
            await service.post_message(session.id, "answer me")

        messages = service.session_messages(session.id)
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[1].content == f"{FAILED_REPLY_PREFIX}model is down)"
        assert messages[1].metadata["error"] == "RuntimeError"
        assert service.get_session(session.id).message_count == 2

    async def test_the_conversation_can_continue_after_a_failure(self):
        calls = {"n": 0}
        recorder = _Recorder()

        async def flaky(inputs, emit=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return await recorder(inputs, emit)

        service = SessionService(MemorySessionStore(), flaky)
        session = service.create_session()

        with pytest.raises(RuntimeError):
            await service.post_message(session.id, "first try")
        result = await service.post_message(session.id, "second try")

        assert result["assistant"].content == "ok"
        assert len(service.session_messages(session.id)) == 4


# ---------------------------------------------------------------------------
# The title, and the serialisation that makes history trustworthy
# ---------------------------------------------------------------------------


class TestTheTitle:
    async def test_the_first_message_names_an_unnamed_conversation(self):
        service = _service(MemorySessionStore(), _Recorder())
        session = service.create_session()

        text = "Explain quantum entanglement in one sentence, please."
        result = await service.post_message(session.id, text)

        assert result["session"].title == " ".join(text.split())[:40]

    async def test_a_title_the_user_set_is_never_overwritten(self):
        service = _service(MemorySessionStore(), _Recorder())
        session = service.create_session(title="Mine")

        result = await service.post_message(session.id, "another question")

        assert result["session"].title == "Mine"

    async def test_auto_titling_can_be_switched_off(self):
        service = _service(MemorySessionStore(), _Recorder(), title_from_message=0)
        session = service.create_session()

        result = await service.post_message(session.id, "a question")

        assert result["session"].title == ""

    async def test_a_multiline_question_becomes_a_single_line_title(self):
        service = _service(MemorySessionStore(), _Recorder())
        session = service.create_session()

        result = await service.post_message(session.id, "first line\nsecond line")

        assert result["session"].title == "first line second line"


class TestConcurrentTurns:
    async def test_two_turns_of_one_session_never_interleave(self):
        store = MemorySessionStore()
        session = store.create()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        snapshots: list[dict] = []

        async def run_fn(inputs, emit=None):
            snapshots.append(dict(inputs))
            if len(snapshots) == 1:
                first_started.set()
                await release_first.wait()
            return {"result": f"reply-{len(snapshots)}"}

        service = SessionService(store, run_fn)
        first = asyncio.create_task(service.post_message(session.id, "first"))
        await asyncio.wait_for(first_started.wait(), timeout=5.0)
        second = asyncio.create_task(service.post_message(session.id, "second"))
        await asyncio.sleep(0.05)

        assert len(snapshots) == 1, "the second turn started while the first was in flight"

        release_first.set()
        await asyncio.gather(first, second)

        assert len(snapshots) == 2
        assert snapshots[0]["history"] == ""
        assert snapshots[1]["history"] == "user: first\nassistant: reply-1\n"
        assert snapshots[1]["text"] == "second"
        assert [m.content for m in store.messages(session.id)] == [
            "first",
            "reply-1",
            "second",
            "reply-2",
        ]

    async def test_different_sessions_do_not_block_each_other(self):
        store = MemorySessionStore()
        blocking_session = store.create()
        other_session = store.create()
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def run_fn(inputs, emit=None):
            if inputs["text"] == "blocking":
                first_started.set()
                await release_first.wait()
            return {"result": "ok"}

        service = SessionService(store, run_fn)
        blocking = asyncio.create_task(
            service.post_message(blocking_session.id, "blocking")
        )
        await asyncio.wait_for(first_started.wait(), timeout=5.0)

        # A second conversation must not be queued behind the first one.
        result = await asyncio.wait_for(
            service.post_message(other_session.id, "independent"), timeout=5.0
        )
        assert result["assistant"].content == "ok"

        release_first.set()
        await blocking


class TestTheSessionHelpers:
    async def test_the_service_delegates_create_list_get_and_delete(self):
        service = _service(MemorySessionStore(), _Recorder())
        session = service.create_session(title="Helpers")

        assert [s.id for s in service.list_sessions()] == [session.id]
        assert service.get_session(session.id).title == "Helpers"
        assert service.delete_session(session.id) is True
        with pytest.raises(SessionNotFound):
            service.get_session(session.id)

    async def test_the_session_snapshot_matches_the_transcript(self):
        service = _service(MemorySessionStore(), _Recorder())
        session = service.create_session()

        for question in ("one", "two", "three"):
            result = await service.post_message(session.id, question)

        assert result["session"].message_count == 6
        assert len(service.session_messages(session.id)) == 6
        assert [m.role for m in service.session_messages(session.id)] == [
            "user",
            "assistant",
        ] * 3


class TestTheEmitCallback:
    async def test_progress_events_reach_the_caller_and_do_not_change_the_reply(self):
        events: list[dict] = []

        async def streaming_run_fn(inputs, emit=None):
            assert emit is not None, "a streaming caller must get the callback"
            emit({"type": "progress", "text": "working"})
            emit({"type": "progress", "text": "almost"})
            return {"result": "done"}

        service = SessionService(MemorySessionStore(), streaming_run_fn)
        session = service.create_session()

        result = await service.post_message(
            session.id, "hi", emit=events.append
        )

        assert events == [
            {"type": "progress", "text": "working"},
            {"type": "progress", "text": "almost"},
        ]
        assert result["assistant"].content == "done"

    async def test_a_plain_run_fn_is_still_callable_when_nothing_streams(self):
        async def one_argument_run_fn(inputs):
            return {"result": f"echo: {inputs['text']}"}

        service = SessionService(MemorySessionStore(), one_argument_run_fn)
        session = service.create_session()

        result = await service.post_message(session.id, "hello")

        assert result["assistant"].content == "echo: hello"
