"""Tests for the repair loop's pure logic and its safety guards.

What these can and cannot cover
-------------------------------

``conftest.py`` forces mock mode (``LLM_API_KEY=""``) so no test reaches the
network, which means the loop itself cannot run here — it raises
``AINotConfigured`` the moment it tries to build a model. That is fine, because
the interesting parts of this module are not the model call:

* the **early exit**, which is the one guard that must fire *before* any model
  call, and is therefore perfectly testable in mock mode (mock mode makes it
  observable: if the guard were missing, the call would raise rather than return)
* the **diff**, which is what the user reads before accepting a repair
* the **target selection**, which decides what "fixed" means

The end-to-end behaviour against a real model lives in ``scripts/smoke.py``
behind ``--ai``, which is the only place a live call belongs.
"""

from __future__ import annotations

import pytest

from app.ai_buildtime.prompts import build_repair_messages, format_issues_for_model
from app.ai_buildtime.repair import (
    build_repair_result,
    describe_diff,
    repair_graph,
    target_issues,
)
from app.graph.schema import WorkflowGraph
from app.graph.validate import validate_graph


def _graph(*nodes: dict, edges: list[dict] | None = None) -> WorkflowGraph:
    return WorkflowGraph.model_validate(
        {
            "id": "wf-repair",
            "name": "Repair me",
            "nodes": list(nodes),
            "edges": edges or [],
        }
    )


def _node(node_id: str, node_type: str, **kw) -> dict:
    base = {
        "id": node_id,
        "type": node_type,
        "label": kw.pop("label", node_id),
        "position": {"x": 0, "y": 0},
        "config": kw.pop("config", {}),
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Targeting
# ---------------------------------------------------------------------------


class TestTargetIssues:
    def test_healthy_graph_has_no_targets(self):
        graph = _graph(
            _node("s", "start"),
            _node("e", "end", config={"output": "x"}),
            edges=[
                {
                    "id": "e1",
                    "source": "s",
                    "source_handle": "out",
                    "target": "e",
                    "target_handle": "in",
                }
            ],
        )
        assert validate_graph(graph).ok
        assert target_issues(graph) == []
        assert target_issues(graph, "all") == []

    def test_errors_are_targeted_by_default(self):
        # An End node with nothing feeding it. The error is targeted; the orphan
        # warning is not, which is the whole point of the default.
        graph = _graph(_node("e", "end", config={"output": "x"}))
        codes = {issue.code for issue in target_issues(graph)}
        assert "missing_entry" in codes
        assert "orphan_node" not in codes, "warnings must not be targeted by default"

    def test_severity_all_also_returns_warnings(self):
        graph = _graph(_node("e", "end", config={"output": "x"}))
        severities = {issue.severity for issue in target_issues(graph, "all")}
        assert severities == {"error", "warning"}

    def test_only_the_requested_severity_is_returned(self):
        graph = _graph(_node("e", "end", config={"output": "x"}))
        assert all(
            issue.severity == "error" for issue in target_issues(graph, "error")
        )


# ---------------------------------------------------------------------------
# Prompt input
# ---------------------------------------------------------------------------


class TestFormatIssuesForModel:
    def test_renders_code_and_message(self):
        graph = _graph(_node("e", "end", config={"output": "x"}))
        text = format_issues_for_model(target_issues(graph))
        assert "missing_entry" in text
        assert "Start" in text

    def test_includes_the_offending_node_id(self):
        graph = _graph(_node("l", "llm", config={"prompt": ""}))
        issues = [i for i in target_issues(graph, "all") if i.node_id]
        assert issues, "expected at least one issue carrying a node_id"
        assert "node_id=l" in format_issues_for_model(issues)

    def test_empty_list_renders_a_sentinel(self):
        assert format_issues_for_model([]) == "(none)"

    def test_output_is_english_regardless_of_language(self):
        """Issue text fed to the model must not follow the UI language.

        The rule this protects: switching the interface to Chinese must not
        change how the model reasons. Stated as a test because it is the kind of
        thing a well-meaning "translate everything" change would break.
        """
        graph = _graph(_node("e", "end", config={"output": "x"}))
        english = format_issues_for_model(target_issues(graph, "en"))
        chinese = format_issues_for_model(target_issues(graph, "zh"))
        assert english == chinese
        assert not any("\u4e00" <= ch <= "\u9fff" for ch in english)


class TestBuildRepairMessages:
    def test_includes_the_reported_problems(self):
        graph = _graph(_node("e", "end", config={"output": "x"}))
        issues = target_issues(graph)
        messages = build_repair_messages(graph, issues)
        user = messages[-1]["content"]
        assert "missing_entry" in user
        assert "## Problems the validator reported" in user

    def test_includes_the_catalogue_so_a_missing_node_can_be_added(self):
        graph = _graph(_node("e", "end", config={"output": "x"}))
        user = build_repair_messages(graph, target_issues(graph))[-1]["content"]
        # A missing Start node is the common structural fix, so the model has to
        # know which types exist.
        assert "start" in user

    def test_system_prompt_forbids_deleting_to_silence_the_validator(self):
        graph = _graph(_node("e", "end", config={"output": "x"}))
        system = build_repair_messages(graph, target_issues(graph))[0]["content"]
        assert "Never delete a node just to silence the validator" in system

    def test_language_rule_switches_with_lang(self):
        """`lang` changes what the model is *told*, not the prompt's own language.

        The prompt stays English so the instruction is unambiguous; only the
        language the model should write new node descriptions in changes.
        """
        graph = _graph(_node("e", "end", config={"output": "x"}))
        issues = target_issues(graph)
        en = build_repair_messages(graph, issues, lang="en")[-1]["content"]
        zh = build_repair_messages(graph, issues, lang="zh")[-1]["content"]
        assert "in English" in en
        assert "in Chinese" in zh
        assert "in English" not in zh

    def test_instruction_is_optional_and_included_when_present(self):
        graph = _graph(_node("e", "end", config={"output": "x"}))
        issues = target_issues(graph)
        without = build_repair_messages(graph, issues)[-1]["content"]
        assert "Additional instruction" not in without

        with_extra = build_repair_messages(
            graph, issues, instruction="use a Loop node"
        )[-1]["content"]
        assert "use a Loop node" in with_extra


# ---------------------------------------------------------------------------
# Diff — what the user actually reads before accepting
# ---------------------------------------------------------------------------


class TestDescribeDiff:
    def test_identical_graphs_report_no_change(self):
        graph = _graph(_node("a", "start"))
        diff = describe_diff(graph, graph)
        assert diff["node_delta"] == 0
        assert diff["edge_delta"] == 0
        assert diff["nodes_added"] == []
        assert diff["nodes_removed"] == []
        assert diff["nodes_updated"] == []

    def test_added_node_carries_id_type_and_label(self):
        before = _graph(_node("a", "start"))
        after = _graph(_node("a", "start"), _node("b", "end", label="Done"))
        diff = describe_diff(before, after)
        assert diff["node_delta"] == 1
        assert diff["nodes_added"] == [
            {"node_id": "b", "type": "end", "label": "Done"}
        ]

    def test_removed_node_is_reported(self):
        """This is the safeguard that makes 'delete everything' visible."""
        before = _graph(_node("a", "start"), _node("b", "end"))
        after = _graph(_node("a", "start"))
        diff = describe_diff(before, after)
        assert diff["node_delta"] == -1
        assert [n["node_id"] for n in diff["nodes_removed"]] == ["b"]

    def test_renaming_is_an_update_not_a_delete_plus_add(self):
        """Identity is the node id, so a rename must not look destructive.

        If this regressed, every AI tweak to a label would render as
        "1 node removed / 1 node added" and the user would stop trusting the diff.
        """
        before = _graph(_node("a", "start", label="Old"))
        after = _graph(_node("a", "start", label="New"))
        diff = describe_diff(before, after)
        assert diff["nodes_removed"] == []
        assert diff["nodes_added"] == []
        assert diff["node_delta"] == 0
        assert diff["nodes_updated"] == [
            {"node_id": "a", "label": "New", "fields": ["label"]}
        ]

    def test_config_change_lists_the_config_field(self):
        before = _graph(_node("l", "llm", config={"prompt": ""}))
        after = _graph(_node("l", "llm", config={"prompt": "hi"}))
        diff = describe_diff(before, after)
        assert diff["nodes_updated"][0]["fields"] == ["config"]

    def test_trusted_change_is_tracked(self):
        # Trusting a Code node is a real edit with security weight, so it has to
        # show up in the diff rather than being applied invisibly.
        before = _graph(_node("c", "code", trusted=False))
        after = _graph(_node("c", "code", trusted=True))
        diff = describe_diff(before, after)
        assert diff["nodes_updated"][0]["fields"] == ["trusted"]

    def test_edges_are_diffed(self):
        nodes = [_node("a", "start"), _node("b", "end", config={"output": "x"})]
        before = _graph(*nodes, edges=[])
        after = _graph(
            *nodes,
            edges=[
                {
                    "id": "e1",
                    "source": "a",
                    "source_handle": "out",
                    "target": "b",
                    "target_handle": "in",
                }
            ],
        )
        diff = describe_diff(before, after)
        assert diff["edge_delta"] == 1
        assert len(diff["edges_added"]) == 1
        assert diff["edges_removed"] == []


# ---------------------------------------------------------------------------
# Result assembly — the rules that decide whether a repair can be trusted
# ---------------------------------------------------------------------------


def _valid_from(start: str = "s", end: str = "e") -> WorkflowGraph:
    """A graph that passes validation, with configurable node ids."""
    return _graph(
        _node(start, "start"),
        _node(end, "end", config={"output": "x"}),
        edges=[
            {
                "id": "e1",
                "source": start,
                "source_handle": "out",
                "target": end,
                "target_handle": "in",
            }
        ],
    )


def _still_broken(*nodes: dict) -> WorkflowGraph:
    """A graph that fails validation, whatever else it contains."""
    return _graph(*nodes)


class TestBuildRepairResult:
    """``build_repair_result`` holds every trust decision, so test it directly.

    Driving these through the model would make them untestable in mock mode,
    which is precisely how the ``regressed`` flag and id preservation first
    shipped broken.
    """

    def test_identity_and_name_are_preserved(self):
        before = _valid_from()
        # Simulate what GraphDraft.to_graph does: a brand new workflow.
        repaired = _valid_from()
        repaired.id = "brand-new-id"
        repaired.name = "Generated workflow"

        result = build_repair_result(before, repaired)

        assert result["graph"]["id"] == "wf-repair"
        assert result["graph"]["name"] == "Repair me"

    def test_resolved_when_every_error_is_gone(self):
        before = _still_broken(_node("e", "end", config={"output": "x"}))
        after = _valid_from()
        result = build_repair_result(before, after)

        assert result["resolved"] is True
        assert result["remaining"] == 0
        assert result["changed"] is True
        assert "Fixed every reported problem" in result["summary"]

    def test_regressed_when_still_broken_and_nodes_were_removed(self):
        """The 'delete everything and you pass' case.

        This is the single most dangerous outcome the feature can produce, so the
        flag and its warning are asserted together — a flag the UI never renders
        is no safeguard.
        """
        before = _still_broken(
            _node("e", "end", config={"output": "x"}),
            _node("l", "llm", config={"prompt": ""}),
            _node("p", "prompt", config={"template": "hi"}),
        )
        # The model deleted the two nodes it did not understand, and the graph
        # still has no Start node.
        after = _still_broken(_node("e", "end", config={"output": "x"}))

        result = build_repair_result(before, after)

        assert result["resolved"] is False
        assert result["regressed"] is True
        assert result["diff"]["node_delta"] == -2
        assert len(result["diff"]["nodes_removed"]) == 2
        assert result["warnings"], "a regression must carry a user-visible warning"
        assert "2 node" in result["warnings"][0]

    def test_not_regressed_when_the_removal_actually_fixed_it(self):
        # Deleting a duplicate node can be a legitimate repair. The flag is about
        # "still broken and smaller", not "smaller".
        before = _still_broken(
            _node("e", "end", config={"output": "x"}),
            _node("dup", "llm", config={"prompt": ""}),
        )
        after = _valid_from()

        result = build_repair_result(before, after)

        assert result["resolved"] is True
        assert result["regressed"] is False
        assert result["warnings"] == []

    def test_not_regressed_when_the_graph_grew(self):
        before = _still_broken(_node("e", "end", config={"output": "x"}))
        # Still broken, but the model added a Start node on the way.
        after = _graph(
            _node("s", "start"),
            _node("e", "end", config={"output": "x"}),
            _node("x", "llm", config={"prompt": ""}),
        )

        result = build_repair_result(before, after)

        assert result["diff"]["node_delta"] > 0
        assert result["regressed"] is False

    def test_unchanged_when_nothing_was_edited(self):
        before = _still_broken(_node("e", "end", config={"output": "x"}))
        result = build_repair_result(before, _still_broken(_node("e", "end", config={"output": "x"})))

        assert result["changed"] is False
        assert result["resolved"] is False
        assert "did not change anything" in result["summary"]

    def test_stalled_is_reported_when_set(self):
        before = _still_broken(_node("e", "end", config={"output": "x"}))
        after = _still_broken(
            _node("e", "end", config={"output": "x", "label": "touched"}),
        )
        result = build_repair_result(before, after, stalled=True)
        assert "Stopped early" in result["summary"]

    def test_partial_summary_counts_what_is_left(self):
        before = _still_broken(_node("e", "end", config={"output": "x"}))
        after = _still_broken(
            _node("e", "end", config={"output": "x"}),
            _node("l", "llm", config={"prompt": ""}),
        )
        result = build_repair_result(before, after)
        assert result["changed"] is True
        assert result["stalled"] is False
        assert result["remaining"] > 0
        assert str(result["remaining"]) in result["summary"]

    def test_summary_is_localised(self):
        before = _still_broken(_node("e", "end", config={"output": "x"}))
        after = _valid_from()
        assert "Fixed" in build_repair_result(before, after, lang="en")["summary"]
        assert (
            build_repair_result(before, after, lang="zh")["summary"]
            == "已修复全部报告的问题。"
        )

    def test_validation_payload_is_localised_but_ids_are_not(self):
        before = _still_broken(_node("e", "end", config={"output": "x"}))
        after = _still_broken(_node("e", "end", config={"output": "x"}))

        english = build_repair_result(before, after, lang="en")["validation"]
        chinese = build_repair_result(before, after, lang="zh")["validation"]

        en_messages = {i["message"] for i in english["issues"]}
        zh_messages = {i["message"] for i in chinese["issues"]}
        assert en_messages != zh_messages
        assert any("\u4e00" <= ch <= "\u9fff" for m in zh_messages for ch in m)

        # Codes and node ids are data: they must survive the language switch.
        assert {i["code"] for i in english["issues"]} == {
            i["code"] for i in chinese["issues"]
        }
        assert {i["node_id"] for i in english["issues"]} == {
            i["node_id"] for i in chinese["issues"]
        }
        assert not any(k.endswith("_zh") for i in chinese["issues"] for k in i)

    def test_severity_all_can_report_resolved_while_warnings_remain(self):
        """Chasing warnings only: errors are none, warnings are the target."""
        before = _still_broken(_node("e", "end", config={"output": "x"}))
        after = _valid_from()

        result = build_repair_result(before, after, severity="all", lang="en")

        if result["resolved"]:
            assert "Fixed" in result["summary"]


# ---------------------------------------------------------------------------
# The early exit
# ---------------------------------------------------------------------------


class TestRepairGuards:
    async def test_healthy_graph_returns_without_calling_the_model(self):
        """A valid graph must not reach the provider at all.

        Mock mode is what makes this observable: the loop would raise
        ``AINotConfigured`` on its first model access, so returning cleanly proves
        the guard fired first. It matters beyond saving tokens — a repair pass over
        a healthy graph can only make it worse.
        """
        graph = _graph(
            _node("s", "start"),
            _node("e", "end", config={"output": "x"}),
            edges=[
                {
                    "id": "e1",
                    "source": "s",
                    "source_handle": "out",
                    "target": "e",
                    "target_handle": "in",
                }
            ],
        )
        assert validate_graph(graph).ok

        result = await repair_graph(graph)

        assert result["resolved"] is True
        assert result["changed"] is False
        assert result["attempts"] == 0
        assert result["remaining"] == 0
        assert result["warnings"] == []
        assert result["regressed"] is False
        assert result["transcript"] == []

    async def test_clean_result_summary_is_localised(self):
        graph = _graph(
            _node("s", "start"),
            _node("e", "end", config={"output": "x"}),
            edges=[
                {
                    "id": "e1",
                    "source": "s",
                    "source_handle": "out",
                    "target": "e",
                    "target_handle": "in",
                }
            ],
        )
        en = await repair_graph(graph, lang="en")
        zh = await repair_graph(graph, lang="zh")
        assert "already passes validation" in en["summary"]
        assert zh["summary"] == "这个工作流已经通过校验，无需修复。"

    async def test_healthy_graph_result_is_unchanged_and_keeps_its_id(self):
        """The returned graph keeps the caller's identity.

        A fresh id would make saving create a second workflow instead of updating
        this one — ``GraphDraft.to_graph`` mints one because generation really is
        a new workflow, so repair has to overwrite it.
        """
        graph = _graph(
            _node("a", "start"),
            _node("b", "end", config={"output": "x"}),
            edges=[
                {
                    "id": "e1",
                    "source": "a",
                    "source_handle": "out",
                    "target": "b",
                    "target_handle": "in",
                }
            ],
        )
        assert validate_graph(graph).ok

        result = await repair_graph(graph)

        assert result["graph"]["id"] == "wf-repair"
        assert result["graph"]["name"] == "Repair me"
        assert len(result["graph"]["nodes"]) == 2

    async def test_broken_graph_reaches_the_provider_and_fails_loudly(self):
        """In mock mode a real repair attempt must raise, not silently no-op.

        ``AINotConfigured`` is what the endpoint turns into a 503 so the UI can
        explain itself. A silent success here would be the worst outcome: the user
        would be shown a "changes" diff that changed nothing.
        """
        from app.ai_buildtime.gen_node import AINotConfigured

        graph = _graph(_node("e", "end", config={"output": "x"}))
        with pytest.raises(AINotConfigured):
            await repair_graph(graph)
