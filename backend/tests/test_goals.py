"""Goals: the plan outlives the run that made it.

The claim task 21 makes is that an agent knows what it is doing and how far it
got. Half of that is a data shape and half is durability, and the durability half
is the one that needs a run boundary to test — a single-process test would pass
against a plan that only ever lived in memory.

So most of this file is two-phase: do something, then read it back through a
store that was opened afterwards. That is what a restart looks like from here.

What is imported from upstream rather than re-declared
------------------------------------------------------
`Todo` (the item shape) and the three statuses come from
`langchain.agents.middleware.todo`. Those are pinned by
:class:`TestUpstreamShapesAreReused` so a future edit that re-types them fails
here — re-typing is how the two drift apart.
"""

from __future__ import annotations

import pytest

from app.memory.goals import (
    GOALS_SEGMENT,
    GOAL_ACTIVE,
    GOAL_ABANDONED,
    GOAL_DONE,
    GoalError,
    build_goal,
    is_finished,
    next_item,
    normalize_items,
    progress,
    reconcile_status,
    summarize,
    validate_objective,
)
from app.memory.goals_node import (
    MAX_LIMIT,
    MODE_ADVANCE,
    MODE_SHOW,
    MODE_START,
    GoalNode,
    _namespace,
)
from app.graph.schema import NodeSpec

# ---------------------------------------------------------------------------
# A node context, reduced to what these nodes touch
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, config: dict, inputs: dict | None = None, workflow_id="wf-goal"):
        self.node = NodeSpec(id="g1", type="goal", label="Goal", config=config)
        self.config = config
        self.inputs = inputs or {}
        self.state = {"workflow_id": workflow_id}
        from app.settings import get_settings

        self.settings = get_settings()
        self.emit = lambda *args, **kwargs: None
        self.logs: list[tuple[str, str]] = []

        async def log(text: str, level: str = "info") -> None:
            self.logs.append((level, text))

        self.log = log
        self.node_outputs = None

    @property
    def vars(self) -> dict:
        return self.state.get("vars") or {}

    @property
    def messages(self) -> list:
        return []


def _node() -> GoalNode:
    return GoalNode(NodeSpec(id="g1", type="goal", label="Goal", config={}))


@pytest.fixture
def store():
    """A fresh in-memory provider for one test, restored afterwards."""
    from app.memory import InMemoryStoreProvider, set_memory_store_provider

    provider = InMemoryStoreProvider()
    set_memory_store_provider(provider)
    yield provider
    set_memory_store_provider(None)


# ---------------------------------------------------------------------------
# The upstream shapes are actually reused
# ---------------------------------------------------------------------------


class TestUpstreamShapesAreReused:
    def test_the_item_type_is_upstreams(self):
        """Not a lookalike. If this fails, the two have been allowed to diverge,
        and the model-facing schema no longer matches the one upstream tunes
        prompts for."""
        from langchain.agents.middleware.todo import Todo as UpstreamTodo

        from app.memory.goals import Todo as Ours

        assert Ours is UpstreamTodo

    def test_the_status_set_is_upstreams(self):
        from typing import get_args

        from langchain.agents.middleware.todo import Todo as UpstreamTodo

        upstream = set(get_args(UpstreamTodo.__annotations__["status"]))
        assert upstream == {"pending", "in_progress", "completed"}

    def test_the_model_facing_prompts_exist_upstream(self):
        """Imported, not paraphrased. These are tuned text — when *not* to use a
        plan, "do not batch completions", "always at least one in_progress" — and
        a copy would lose the next improvement."""
        from langchain.agents.middleware.todo import (
            WRITE_TODOS_SYSTEM_PROMPT,
            WRITE_TODOS_TOOL_DESCRIPTION,
        )

        assert "write_todos" in WRITE_TODOS_SYSTEM_PROMPT
        assert "do not batch" in WRITE_TODOS_SYSTEM_PROMPT.lower()
        assert len(WRITE_TODOS_TOOL_DESCRIPTION) > 1000


# ---------------------------------------------------------------------------
# Building a goal
# ---------------------------------------------------------------------------


class TestBuilding:
    def test_a_goal_gets_an_id_and_a_timestamp(self):
        goal = build_goal("Reconcile orders", ["one"])
        assert goal["id"]
        assert goal["status"] == GOAL_ACTIVE
        assert goal["created_at"] > 0

    def test_an_empty_objective_is_refused(self):
        with pytest.raises(GoalError, match="one sentence"):
            build_goal("   ")

    def test_an_over_long_objective_is_refused_and_says_where_to_put_it(self):
        with pytest.raises(GoalError, match="items"):
            validate_objective("x" * 3_000)

    @pytest.mark.parametrize(
        "given,expected",
        [
            (["a", "b"], ["a", "b"]),
            ("a\nb", ["a", "b"]),
            ([{"content": "a"}], ["a"]),
            ([{"text": "a"}], ["a"]),
            ([{"task": "a"}], ["a"]),
            (None, []),
        ],
    )
    def test_the_forms_a_model_produces_are_all_accepted(self, given, expected):
        """A model will return plain strings whatever the schema says, so refusing
        them would push the same coercion into every caller."""
        assert [item["content"] for item in normalize_items(given)] == expected

    def test_an_invented_status_becomes_pending(self):
        """Models write "done", "todo", "in progress". Failing a whole plan over
        one is worse than showing it as not-yet-started, which is the safe reading
        because it will be revisited."""
        items = normalize_items([{"content": "a", "status": "done"}])
        assert items[0]["status"] == "pending"

    def test_a_recognised_status_survives(self):
        for status in ("pending", "in_progress", "completed"):
            assert normalize_items([{"content": "a", "status": status}])[0]["status"] == status

    def test_a_non_list_is_refused_with_the_expected_shape(self):
        with pytest.raises(GoalError, match='"content"'):
            normalize_items({"a": 1})

    def test_an_item_with_no_content_is_refused(self):
        with pytest.raises(GoalError, match="no content"):
            normalize_items([{"status": "pending"}])


# ---------------------------------------------------------------------------
# Reading progress
# ---------------------------------------------------------------------------


class TestProgress:
    def test_counts_by_status(self):
        goal = build_goal("x", ["a", "b", "c"])
        goal["items"][0]["status"] = "completed"
        goal["items"][1]["status"] = "in_progress"
        assert progress(goal) == {"pending": 1, "in_progress": 1, "completed": 1, "total": 3}

    def test_an_in_progress_item_is_next_in_preference_to_a_pending_one(self):
        """A plan that has been worked on should be resumed, not restarted."""
        goal = build_goal("x", ["a", "b"])
        goal["items"][1]["status"] = "in_progress"
        assert next_item(goal)["content"] == "b"

    def test_without_an_in_progress_item_the_first_pending_wins(self):
        goal = build_goal("x", ["a", "b"])
        assert next_item(goal)["content"] == "a"

    def test_no_next_item_when_everything_is_done(self):
        goal = build_goal("x", ["a"])
        goal["items"][0]["status"] = "completed"
        assert next_item(goal) is None

    def test_an_empty_plan_is_not_finished(self):
        """Either it has not been planned or it was created by mistake. Calling
        either 'done' lets a run declare success without doing anything."""
        assert is_finished(build_goal("x", [])) is False

    def test_a_fully_completed_plan_is_finished(self):
        goal = build_goal("x", ["a", "b"])
        for item in goal["items"]:
            item["status"] = "completed"
        assert is_finished(goal) is True

    def test_completing_the_last_item_marks_the_goal_done(self):
        """Done in `reconcile_status` rather than by the caller, so 'the last item
        completed' and 'the goal is done' cannot disagree."""
        goal = build_goal("x", ["a"])
        goal["items"][0]["status"] = "completed"
        assert reconcile_status(goal)["status"] == GOAL_DONE

    def test_reopening_an_item_makes_the_goal_active_again(self):
        goal = build_goal("x", ["a"])
        goal["items"][0]["status"] = "completed"
        reconcile_status(goal)
        goal["items"][0]["status"] = "pending"
        assert reconcile_status(goal)["status"] == GOAL_ACTIVE

    def test_the_summary_line_carries_the_count(self):
        goal = build_goal("Reconcile orders", ["a", "b"])
        goal["items"][0]["status"] = "completed"
        line = summarize(goal)
        assert "Reconcile orders" in line
        assert "1/2" in line


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------


class TestNamespace:
    def test_goals_live_under_a_fixed_segment(self):
        """So a goal never appears in a memory search of the same scope."""
        assert _namespace("{agent}", "wf", "run") == ("wf", GOALS_SEGMENT)

    def test_a_slash_makes_a_hierarchy(self):
        assert _namespace("{agent}/team-a", "wf", "run") == ("wf", "team-a", GOALS_SEGMENT)

    def test_an_empty_scope_still_lands_somewhere(self):
        assert _namespace("", "wf", "run") == ("wf", GOALS_SEGMENT)

    def test_the_run_placeholder_is_available(self):
        assert _namespace("scratch/{run}", "wf", "r1") == ("scratch", "r1", GOALS_SEGMENT)


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------


class TestStartingAPlan:
    async def test_start_writes_a_plan(self, store):
        node = _node()
        ctx = _Ctx({"mode": MODE_START, "objective": "Do the thing", "items": ["a", "b"]})
        result = await node.run(ctx)

        assert result["out"]["objective"] == "Do the thing"
        assert [item["content"] for item in result["out"]["items"]] == ["a", "b"]

    async def test_the_next_output_is_the_first_step(self, store):
        node = _node()
        ctx = _Ctx({"mode": MODE_START, "objective": "Do it", "items": ["first", "second"]})
        assert (await node.run(ctx))["next"] == "first"

    async def test_the_input_is_used_when_the_objective_field_is_empty(self, store):
        node = _node()
        ctx = _Ctx({"mode": MODE_START, "items": ["a"]}, inputs={"in": "From upstream"})
        assert (await node.run(ctx))["out"]["objective"] == "From upstream"

    async def test_starting_twice_leaves_two_goals(self, store):
        """`start` does not upsert. Resuming a goal and replacing it are different
        intentions, and one call that did either would sometimes do the wrong one.
        """
        node = _node()
        for objective in ("First", "Second"):
            await node.run(_Ctx({"mode": MODE_START, "objective": objective, "items": ["a"]}))

        items = await store.make().asearch(("wf-goal", GOALS_SEGMENT), limit=10)
        assert sorted(item.value["objective"] for item in items) == ["First", "Second"]

    async def test_a_goal_with_no_objective_fails_loudly(self, store):
        """Rather than continuing with no plan while appearing to have one. The
        node's `on_error` is what makes that survivable when it is intended."""
        node = _node()
        with pytest.raises(ValueError, match="could not start a plan"):
            await node.run(_Ctx({"mode": MODE_START, "items": ["a"]}))


class TestAdvancingAPlan:
    async def test_a_step_can_be_marked_done_by_number(self, store):
        node = _node()
        await node.run(_Ctx({"mode": MODE_START, "objective": "x", "items": ["a", "b", "c"]}))

        result = await node.run(_Ctx({"mode": MODE_ADVANCE, "item": "2"}))
        assert result["out"]["items"][1]["status"] == "completed"
        assert result["out"]["items"][0]["status"] == "pending"

    async def test_the_numbers_are_one_based_because_that_is_how_it_is_shown(self, store):
        """A plan displayed as 1..n has to be addressable as 1..n."""
        node = _node()
        await node.run(_Ctx({"mode": MODE_START, "objective": "x", "items": ["first"]}))
        result = await node.run(_Ctx({"mode": MODE_ADVANCE, "item": "1"}))
        assert result["out"]["items"][0]["status"] == "completed"

    async def test_a_step_can_be_found_by_its_text(self, store):
        node = _node()
        await node.run(_Ctx({"mode": MODE_START, "objective": "x", "items": ["a", "b"]}))
        result = await node.run(_Ctx({"mode": MODE_ADVANCE, "item": "b"}))
        assert result["out"]["items"][1]["status"] == "completed"

    async def test_a_prefix_of_a_step_matches(self, store):
        """A model repeating a long step rarely reproduces it exactly."""
        node = _node()
        await node.run(_Ctx(
            {"mode": MODE_START, "objective": "x", "items": ["read the whole order log"]}
        ))
        result = await node.run(_Ctx({"mode": MODE_ADVANCE, "item": "read the whole"}))
        assert result["out"]["items"][0]["status"] == "completed"

    async def test_a_number_beats_a_text_match(self, store):
        """A plan whose steps read '1. check the log' would otherwise match the
        text `1` against the wrong item."""
        node = _node()
        await node.run(_Ctx({"mode": MODE_START, "objective": "x", "items": ["1. check", "b"]}))
        result = await node.run(_Ctx({"mode": MODE_ADVANCE, "item": "1"}))
        assert result["out"]["items"][0]["status"] == "completed"
        assert result["out"]["items"][1]["status"] == "pending"

    async def test_an_empty_step_advances_the_current_one(self, store):
        node = _node()
        await node.run(_Ctx({"mode": MODE_START, "objective": "x", "items": ["a", "b"]}))
        result = await node.run(_Ctx({"mode": MODE_ADVANCE, "item": ""}))
        assert result["out"]["items"][0]["status"] == "completed"

    async def test_a_step_can_be_reopened(self, store):
        node = _node()
        await node.run(_Ctx({"mode": MODE_START, "objective": "x", "items": ["a"]}))
        await node.run(_Ctx({"mode": MODE_ADVANCE, "item": "1"}))
        result = await node.run(
            _Ctx({"mode": MODE_ADVANCE, "item": "1", "item_status": "pending"})
        )
        assert result["out"]["items"][0]["status"] == "pending"
        assert result["out"]["status"] == GOAL_ACTIVE

    async def test_the_next_output_follows_the_progress(self, store):
        node = _node()
        await node.run(_Ctx({"mode": MODE_START, "objective": "x", "items": ["a", "b"]}))
        result = await node.run(_Ctx({"mode": MODE_ADVANCE, "item": "1"}))
        assert result["next"] == "b"

    async def test_an_unknown_step_warns_and_leaves_the_plan_alone(self, store):
        node = _node()
        await node.run(_Ctx({"mode": MODE_START, "objective": "x", "items": ["a"]}))
        ctx = _Ctx({"mode": MODE_ADVANCE, "item": "nonexistent"})
        result = await node.run(ctx)

        assert result["out"]["items"][0]["status"] == "pending"
        assert any(level == "warn" for level, _ in ctx.logs)

    async def test_advancing_with_no_goal_warns_instead_of_failing(self, store):
        """A loop that runs before the plan exists should say so, not break."""
        node = _node()
        ctx = _Ctx({"mode": MODE_ADVANCE, "item": "1"})
        result = await node.run(ctx)

        assert result["out"] is None
        assert any("No goal to advance" in text for _, text in ctx.logs)

    async def test_abandoning_a_goal_is_explicit(self, store):
        """Not derived: 'stopped working on it' is information the plan itself
        does not carry."""
        node = _node()
        await node.run(_Ctx({"mode": MODE_START, "objective": "x", "items": ["a"]}))
        result = await node.run(
            _Ctx({"mode": MODE_ADVANCE, "item": "1", "item_status": "pending",
                  "goal_status": GOAL_ABANDONED})
        )
        assert result["out"]["status"] == GOAL_ABANDONED


class TestShowingTheGoal:
    async def test_show_returns_the_plan_and_what_is_next(self, store):
        node = _node()
        await node.run(_Ctx({"mode": MODE_START, "objective": "x", "items": ["a", "b"]}))
        await node.run(_Ctx({"mode": MODE_ADVANCE, "item": "1"}))

        result = await node.run(_Ctx({"mode": MODE_SHOW}))
        assert result["out"]["objective"] == "x"
        assert result["next"] == "b"
        assert result["progress"] == {"pending": 1, "in_progress": 0, "completed": 1, "total": 2}

    async def test_show_picks_the_most_recently_updated_goal(self, store):
        """A scope accumulates goals across runs, and the useful default for a
        loop is the one it is working on. `updated_at`, not `created_at`, so a
        goal resumed yesterday beats one started this morning and dropped."""
        node = _node()
        await node.run(_Ctx({"mode": MODE_START, "objective": "Older", "items": ["a"]}))
        await node.run(_Ctx({"mode": MODE_START, "objective": "Newer", "items": ["a"]}))

        # Touch the older one.
        older = await store.make().asearch(("wf-goal", GOALS_SEGMENT), limit=10)
        older_id = next(item.key for item in older if item.value["objective"] == "Older")
        await node.run(_Ctx({"mode": MODE_ADVANCE, "goal_id": older_id, "item": "1"}))

        result = await node.run(_Ctx({"mode": MODE_SHOW}))
        assert result["out"]["objective"] == "Older"

    async def test_a_specific_goal_can_be_shown_by_id(self, store):
        node = _node()
        first = await node.run(_Ctx({"mode": MODE_START, "objective": "One", "items": ["a"]}))
        await node.run(_Ctx({"mode": MODE_START, "objective": "Two", "items": ["a"]}))

        result = await node.run(_Ctx({"mode": MODE_SHOW, "goal_id": first["out"]["id"]}))
        assert result["out"]["objective"] == "One"

    async def test_an_empty_scope_reports_nothing_rather_than_failing(self, store):
        node = _node()
        result = await node.run(_Ctx({"mode": MODE_SHOW}))
        assert result["out"] is None
        assert result["next"] == ""


class TestDegrading:
    async def test_off_is_a_warning_and_the_graph_keeps_running(self):
        from app.memory import NoMemoryStoreProvider, set_memory_store_provider

        set_memory_store_provider(NoMemoryStoreProvider())
        try:
            ctx = _Ctx({"mode": MODE_SHOW})
            result = await _node().run(ctx)
            assert result["out"] is None
            assert any("MEMORY_STORE=off" in text for _, text in ctx.logs)
        finally:
            set_memory_store_provider(None)


class TestItSurvivesARestart:
    async def test_a_plan_written_by_one_provider_is_read_by_the_next(self, tmp_path):
        """The property that makes this a goal rather than a state channel: the
        second provider is opened after the first is closed, which is what a
        restart looks like from here."""
        from app.memory import SqliteStoreProvider, set_memory_store_provider

        db = tmp_path / "memory.db"

        first = SqliteStoreProvider(db)
        set_memory_store_provider(first)
        await first.open()
        await _node().run(
            _Ctx({"mode": MODE_START, "objective": "Survives", "items": ["a", "b"]})
        )
        await first.close()

        second = SqliteStoreProvider(db)
        set_memory_store_provider(second)
        await second.open()
        try:
            result = await _node().run(_Ctx({"mode": MODE_SHOW}))
            assert result["out"]["objective"] == "Survives"
            assert result["next"] == "a"
        finally:
            await second.close()
            set_memory_store_provider(None)

    async def test_progress_made_before_a_restart_is_still_there(self, tmp_path):
        from app.memory import SqliteStoreProvider, set_memory_store_provider

        db = tmp_path / "memory.db"

        first = SqliteStoreProvider(db)
        set_memory_store_provider(first)
        await first.open()
        await _node().run(_Ctx({"mode": MODE_START, "objective": "Half done", "items": ["a", "b"]}))
        await _node().run(_Ctx({"mode": MODE_ADVANCE, "item": "1"}))
        await first.close()

        second = SqliteStoreProvider(db)
        set_memory_store_provider(second)
        await second.open()
        try:
            result = await _node().run(_Ctx({"mode": MODE_SHOW}))
            assert result["progress"]["completed"] == 1
            assert result["next"] == "b"
        finally:
            await second.close()
            set_memory_store_provider(None)


class TestGoalsAreNotMemory:
    async def test_a_goal_does_not_appear_in_a_semantic_memory_search(self, store):
        """The segment separates them, but **not** the way it first appears.

        Measured: `BaseStore.search` matches on `namespace_prefix`, so searching
        `("wf-goal",)` also returns items in `("wf-goal", "goals")`. That is the
        right behaviour for memory — an agent's facts are spread across sub-scopes
        and a reader wants all of them — and it means "a different segment keeps
        them apart" is false as a general statement.

        What actually keeps a plan out of a memory read is that the memory node
        searches a namespace that does not contain the goals segment. So this
        asserts the property that does hold.
        """
        from app.graph.schema import NodeSpec as Spec

        from app.memory.nodes import MemoryNode

        await _node().run(
            _Ctx({"mode": MODE_START, "objective": "A plan, not a fact", "items": ["a"]})
        )

        memory = MemoryNode(Spec(id="m1", type="memory", label="M", config={}))
        result = await memory.run(
            _Ctx(
                {"mode": "list", "namespace": "{agent}/facts", "limit": 20},
                workflow_id="wf-goal",
            )
        )
        assert result["out"] == []

    async def test_a_parent_namespace_search_does_reach_goals(self, store):
        """Pinning the measured behaviour rather than leaving it a surprise.

        This is why the fixed `goals` segment exists: it makes the two
        *addressable separately*, which is a weaker and more honest claim than
        "isolated". A deployment that scopes memory to `{agent}` and goals to
        `{agent}` would see plans in memory reads, and that is worth knowing
        before it is discovered in production.
        """
        await _node().run(_Ctx({"mode": MODE_START, "objective": "A plan", "items": ["a"]}))

        # Searching the parent reaches the child. Deliberate upstream semantics.
        found = await store.make().asearch(("wf-goal",), limit=20)
        assert len(found) == 1
        assert found[0].value["objective"] == "A plan"

        # And the goals namespace is addressable on its own.
        goals = await store.make().asearch(("wf-goal", GOALS_SEGMENT), limit=20)
        assert len(goals) == 1

    def test_the_goals_segment_is_a_fixed_name(self):
        """A deployment cannot rename it into the memory namespace by accident,
        and a future change to it fails here rather than silently mixing the two.
        """
        assert GOALS_SEGMENT == "goals"
        assert _namespace("{agent}", "wf", "run")[-1] == GOALS_SEGMENT
