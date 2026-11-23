"""Long-term memory: the seam, the degradation, and the node.

The retrieval engine is upstream and covered upstream, so these do not re-test
search ranking. What they pin is the part that is ours and the part that is easy
to get wrong:

* which store a deployment gets, and that an unknown name is an error rather
  than a silent fallback;
* **memory survives a restart** — the one property that separates memory from
  shared state, and the one that a passing single-process test would not show;
* the two supported degradations (`off`, and no embedding model) keep a graph
  running instead of failing it;
* writing the same fact twice overwrites rather than accumulating.

The sqlite tests are slower than the rest of the suite because opening a store
touches the disk. They are kept because the durability they cover is exactly what
an in-memory test cannot.
"""

from __future__ import annotations

import pathlib

import pytest

from app.graph.schema import NodeSpec
from app.memory import (
    MEMORY_STORE_MEMORY,
    MEMORY_STORE_OFF,
    MEMORY_STORE_SQLITE,
    InMemoryStoreProvider,
    MemoryConfigError,
    NoMemoryStoreProvider,
    SqliteStoreProvider,
    build_memory_store_provider,
    get_embedder,
    set_memory_store_provider,
)
from app.memory.nodes import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MODE_LIST,
    MODE_SEARCH,
    MODE_WRITE,
    MemoryNode,
    _derive_key,
    _resolve_namespace,
)
from app.settings import Settings, get_settings

# ---------------------------------------------------------------------------
# A node context, reduced to what this node touches
# ---------------------------------------------------------------------------


class _Ctx:
    """Stands in for `NodeContext`.

    Hand-written rather than built through the runtime because the runtime needs
    a compiled graph, a checkpointer and a manager to produce one — none of which
    this node reads. Using the real dataclass would test the harness.
    """

    def __init__(self, config: dict, inputs: dict | None = None, workflow_id="wf-test"):
        self.node = NodeSpec(id="m1", type="memory", label="Memory", config=config)
        self.config = config
        self.inputs = inputs or {}
        self.state = {"workflow_id": workflow_id}
        self.settings = get_settings()
        self.emit = lambda *args, **kwargs: None
        self.logs: list[tuple[str, str]] = []

        async def log(text: str, level: str = "info") -> None:
            self.logs.append((level, text))

        self.log = log
        self.node_outputs = None

    @property
    def messages(self) -> list:
        return []


def _settings(**overrides) -> Settings:
    """Settings with memory configured, without reading the environment.

    Constructed with the real class so a renamed field fails here rather than in
    production; only the memory-relevant values are set.
    """
    return Settings(
        memory_store=overrides.get("memory_store", MEMORY_STORE_SQLITE),
        embedding_model=overrides.get("embedding_model", ""),
        embedding_api_key=overrides.get("embedding_api_key", ""),
        llm_api_key=overrides.get("llm_api_key", ""),
        data_dir=overrides.get("data_dir", pathlib.Path(".")),
    )


@pytest.fixture
def store():
    """A fresh in-memory provider installed for one test, then restored."""
    provider = InMemoryStoreProvider()
    set_memory_store_provider(provider)
    yield provider
    set_memory_store_provider(None)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestWhichStore:
    def test_the_default_is_the_durable_one(self):
        """Memory that dies with the process is not memory. Pinned because the
        default is the whole difference between 'remembers' and 'appears to'."""
        assert Settings().memory_store == MEMORY_STORE_SQLITE

    def test_sqlite_resolves_to_a_file_backed_provider(self, tmp_path):
        provider = build_memory_store_provider(_settings(data_dir=tmp_path))
        assert isinstance(provider, SqliteStoreProvider)

    def test_memory_resolves_to_the_in_process_provider(self):
        provider = build_memory_store_provider(
            _settings(memory_store=MEMORY_STORE_MEMORY)
        )
        assert isinstance(provider, InMemoryStoreProvider)

    def test_off_resolves_to_a_provider_that_has_nothing(self):
        provider = build_memory_store_provider(_settings(memory_store=MEMORY_STORE_OFF))
        assert isinstance(provider, NoMemoryStoreProvider)

    def test_an_unknown_name_is_an_error_not_a_fallback(self):
        """A typo for `sqlite` that quietly became `memory` would give an operator
        a system they believe is durable and is not."""
        with pytest.raises(MemoryConfigError, match="not a known store"):
            build_memory_store_provider(_settings(memory_store="sqllite"))

    def test_the_error_names_every_supported_value(self):
        with pytest.raises(MemoryConfigError) as caught:
            build_memory_store_provider(_settings(memory_store="postgres"))
        message = str(caught.value)
        for value in (MEMORY_STORE_SQLITE, MEMORY_STORE_MEMORY, MEMORY_STORE_OFF):
            assert value in message


class TestWhichEmbedder:
    def test_no_model_means_no_semantic_index(self):
        """A supported configuration, not a failure: `IndexConfig(embed=None)` is
        how upstream says 'no vector index'."""
        assert get_embedder(_settings()) is None

    def test_a_model_with_no_key_is_still_none_and_says_so(self, caplog):
        """The symptom of a missing key is search matching literally, which looks
        like a bug in search. Hence the warning."""
        with caplog.at_level("WARNING"):
            assert get_embedder(_settings(embedding_model="embedding-3")) is None
        assert "no embedding key" in caplog.text

    def test_the_chat_key_is_reused_when_no_embedding_key_is_set(self):
        """One provider, one credential — asking for the same secret twice is how
        a deployment ends up configured halfway."""
        embedder = get_embedder(
            _settings(embedding_model="embedding-3", llm_api_key="sk-test")
        )
        assert embedder is not None

    def test_a_separate_key_wins(self):
        embedder = get_embedder(
            _settings(
                embedding_model="embedding-3",
                embedding_api_key="embed-only",
                llm_api_key="chat-only",
            )
        )
        assert embedder is not None
        assert embedder.openai_api_key.get_secret_value() == "embed-only"


# ---------------------------------------------------------------------------
# The node: writing
# ---------------------------------------------------------------------------


class TestWriting:
    async def test_a_fact_written_is_a_fact_found(self, store):
        node = MemoryNode(NodeSpec(id="w", type="memory", label="W", config={}))
        await node.run(_Ctx({"mode": MODE_WRITE, "text": "订单号以 ACME- 开头"}))

        found = await node.run(_Ctx({"mode": MODE_LIST}))
        assert found["out"] == ["订单号以 ACME- 开头"]

    async def test_writing_the_same_fact_twice_does_not_accumulate(self, store):
        """The failure this prevents is quiet and looks like broken search: a
        nightly graph piles up five copies of one sentence and the reader sees
        five identical results."""
        node = MemoryNode(NodeSpec(id="w", type="memory", label="W", config={}))
        for _ in range(3):
            ctx = _Ctx({"mode": MODE_WRITE, "text": "同一件事"})
            await node.run(ctx)

        found = await node.run(_Ctx({"mode": MODE_LIST, "limit": 50}))
        assert found["out"] == ["同一件事"]

    def test_a_derived_key_is_stable_for_the_same_text(self):
        assert _derive_key("abc") == _derive_key("abc")
        assert _derive_key("abc") != _derive_key("abd")

    async def test_an_explicit_key_overwrites_that_entry(self, store):
        node = MemoryNode(NodeSpec(id="w", type="memory", label="W", config={}))
        await node.run(_Ctx({"mode": MODE_WRITE, "text": "first", "key": "slot"}))
        await node.run(_Ctx({"mode": MODE_WRITE, "text": "second", "key": "slot"}))

        found = await node.run(_Ctx({"mode": MODE_LIST}))
        assert found["out"] == ["second"]

    async def test_the_input_is_used_when_the_text_field_is_empty(self, store):
        """Lets the node sit inline in a chain without a template."""
        node = MemoryNode(NodeSpec(id="w", type="memory", label="W", config={}))
        ctx = _Ctx({"mode": MODE_WRITE}, inputs={"in": "来自上游的事实"})
        await node.run(ctx)

        found = await node.run(_Ctx({"mode": MODE_LIST}))
        assert found["out"] == ["来自上游的事实"]

    async def test_nothing_to_write_is_a_warning_not_a_crash(self, store):
        """A graph must not fail because a branch produced no text."""
        node = MemoryNode(NodeSpec(id="w", type="memory", label="W", config={}))
        ctx = _Ctx({"mode": MODE_WRITE})
        result = await node.run(ctx)

        assert result["out"] == ""
        assert any(level == "warn" for level, _ in ctx.logs)

    async def test_a_non_string_value_round_trips_as_json(self, store):
        """A node upstream may hand over a dict; storing `str(dict)` would make
        the fact unreadable in a store browser."""
        node = MemoryNode(NodeSpec(id="w", type="memory", label="W", config={}))
        await node.run(_Ctx({"mode": MODE_WRITE, "text": {"a": 1}}))

        found = await node.run(_Ctx({"mode": MODE_LIST}))
        assert found["out"] == ['{"a": 1}']


# ---------------------------------------------------------------------------
# The node: reading
# ---------------------------------------------------------------------------


class TestReading:
    async def test_list_returns_everything_in_the_namespace(self, store):
        node = MemoryNode(NodeSpec(id="m", type="memory", label="M", config={}))
        for text in ("一", "二", "三"):
            await node.run(_Ctx({"mode": MODE_WRITE, "text": text}))

        result = await node.run(_Ctx({"mode": MODE_LIST, "limit": 10}))
        assert sorted(result["out"]) == ["一", "三", "二"]

    async def test_a_search_reports_scores(self, store):
        """'Why did it return that' is the first question when a result looks
        wrong, and a score is what answers it."""
        node = MemoryNode(NodeSpec(id="m", type="memory", label="M", config={}))
        await node.run(_Ctx({"mode": MODE_WRITE, "text": "订单号以 ACME- 开头"}))

        result = await node.run(_Ctx({"mode": MODE_SEARCH, "query": "订单号"}))
        assert result["matches"][0]["text"] == "订单号以 ACME- 开头"
        assert isinstance(result["matches"][0]["score"], float)

    async def test_the_query_falls_back_to_the_input(self, store):
        node = MemoryNode(NodeSpec(id="m", type="memory", label="M", config={}))
        await node.run(_Ctx({"mode": MODE_WRITE, "text": "目标事实"}))

        ctx = _Ctx({"mode": MODE_SEARCH}, inputs={"in": "目标"})
        result = await node.run(ctx)
        assert result["out"] == ["目标事实"]

    async def test_a_different_namespace_sees_nothing(self, store):
        """Scopes are what keep one customer's facts out of another's prompt."""
        node = MemoryNode(NodeSpec(id="m", type="memory", label="M", config={}))
        await node.run(_Ctx({"mode": MODE_WRITE, "text": "私事", "namespace": "{agent}/a"}))

        other = await node.run(_Ctx({"mode": MODE_LIST, "namespace": "{agent}/b"}))
        assert other["out"] == []

    async def test_an_exact_filter_narrows_the_result(self, store):
        node = MemoryNode(NodeSpec(id="m", type="memory", label="M", config={}))

        # Two facts, where a filter can only be satisfied by one of them.
        await store.make().aput(
            ("wf-test",), "a", {"text": "偏好邮件", "kind": "preference"}
        )
        await store.make().aput(("wf-test",), "b", {"text": "偏好电话", "kind": "other"})

        result = await node.run(
            _Ctx({"mode": MODE_SEARCH, "query": "偏好", "filter": {"kind": "preference"}})
        )
        assert [match["text"] for match in result["matches"]] == ["偏好邮件"]

    async def test_limit_is_clamped_to_a_ceiling(self, store):
        """Returning everything would put an unbounded amount into the state and,
        from there, into a prompt."""
        node = MemoryNode(NodeSpec(id="m", type="memory", label="M", config={}))
        for index in range(MAX_LIMIT + 5):
            await node.run(_Ctx({"mode": MODE_WRITE, "text": f"fact-{index}"}))

        result = await node.run(_Ctx({"mode": MODE_LIST, "limit": 10_000}))
        assert len(result["out"]) == MAX_LIMIT

    async def test_a_junk_limit_falls_back_and_warns(self, store):
        node = MemoryNode(NodeSpec(id="m", type="memory", label="M", config={}))
        for text in ("a", "b", "c", "d", "e", "f"):
            await node.run(_Ctx({"mode": MODE_WRITE, "text": text}))

        ctx = _Ctx({"mode": MODE_LIST, "limit": "not a number"})
        result = await node.run(ctx)
        assert len(result["out"]) == DEFAULT_LIMIT
        assert any(level == "warn" for level, _ in ctx.logs)


# ---------------------------------------------------------------------------
# The two supported degradations
# ---------------------------------------------------------------------------


class TestDegrading:
    async def test_off_is_a_warning_and_the_graph_keeps_running(self):
        """One node's configuration must not become a graph-wide outage."""
        set_memory_store_provider(NoMemoryStoreProvider())
        try:
            node = MemoryNode(NodeSpec(id="m", type="memory", label="M", config={}))
            ctx = _Ctx({"mode": MODE_SEARCH, "query": "anything"})
            result = await node.run(ctx)

            assert result["out"] == []
            assert any("switched off" in text for _, text in ctx.logs)
        finally:
            set_memory_store_provider(None)

    async def test_no_embedder_still_stores_and_reads(self):
        """A deployment with no embedding key gets literal matching rather than
        an exception — the memory feature stays usable."""
        provider = InMemoryStoreProvider(embed=None, dims=None)
        set_memory_store_provider(provider)
        try:
            node = MemoryNode(NodeSpec(id="m", type="memory", label="M", config={}))
            await node.run(_Ctx({"mode": MODE_WRITE, "text": "订单号以 ACME- 开头"}))

            result = await node.run(_Ctx({"mode": MODE_SEARCH, "query": "订单号以 ACME- 开头"}))
            assert result["out"] == ["订单号以 ACME- 开头"]
        finally:
            set_memory_store_provider(None)


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------


class TestItSurvivesARestart:
    async def test_a_fact_written_by_one_provider_is_read_by_the_next(self, tmp_path):
        """The property that makes this memory rather than shared state.

        Two providers over one file, with the first closed before the second
        opens — which is what a restart looks like from here. A single-provider
        test would pass against a store that never wrote anything to disk.
        """
        db = tmp_path / "memory.db"
        node = MemoryNode(NodeSpec(id="m", type="memory", label="M", config={}))

        first = SqliteStoreProvider(db)
        set_memory_store_provider(first)
        await first.open()
        await node.run(_Ctx({"mode": MODE_WRITE, "text": "重启后还记得"}))
        await first.close()

        assert db.exists(), "nothing was written to disk"

        second = SqliteStoreProvider(db)
        set_memory_store_provider(second)
        await second.open()
        try:
            result = await node.run(_Ctx({"mode": MODE_LIST}))
            assert result["out"] == ["重启后还记得"]
        finally:
            await second.close()
            set_memory_store_provider(None)

    async def test_using_the_sqlite_store_before_opening_it_says_so(self, tmp_path):
        """A test that forgot to open it would otherwise get a store that writes
        nowhere and reads back nothing."""
        provider = SqliteStoreProvider(tmp_path / "memory.db")
        with pytest.raises(MemoryConfigError, match="before it was opened"):
            provider.make()


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------


class TestNamespace:
    def test_the_agent_placeholder_becomes_the_workflow_id(self):
        ctx = _Ctx({}, workflow_id="wf-123")
        assert _resolve_namespace("{agent}", "wf-123", "run-1") == ("wf-123",)

    def test_a_slash_makes_a_hierarchy(self):
        assert _resolve_namespace("{agent}/prefs", "wf", "run") == ("wf", "prefs")

    def test_an_empty_scope_still_lands_somewhere(self):
        """A misspelled token should not collapse every agent into one namespace."""
        assert _resolve_namespace("", "wf", "run") == ("wf",)

    def test_the_run_placeholder_is_available_for_scratch_space(self):
        assert _resolve_namespace("scratch/{run}", "wf", "run-9") == ("scratch", "run-9")

    def test_the_input_token_is_left_literal_on_purpose(self):
        """Memory scoped to one request can never be recalled by another, which
        defeats the point. Leaving the token visible makes that obvious instead of
        silently scoping to nothing."""
        assert _resolve_namespace("{input}", "wf", "run") == ("{input}",)
