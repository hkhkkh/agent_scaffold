"""Repair an invalid workflow by driving the graph-mutation tools.

Why this is a separate module from :mod:`app.ai_buildtime.gen_graph`
---------------------------------------------------------

The two share every mechanism — the same :class:`~app.ai_buildtime.gen_graph.GraphDraft`,
the same ``draft_tools()``, the same ``_dispatch`` — but they have **different
completion criteria**, and that difference is the whole feature:

``generate_graph`` finishes when the model *says* it is done. It is building
something new, so only the user can judge whether the result is right.

``repair_graph`` finishes when **the validator stops complaining**. The goal is
machine-checkable, and that changes the loop twice over:

1. The remaining problems are fed back on *every* iteration, not just the outcome
   of the last mutation. A model that fixed three of five errors is told about the
   other two at once, which is what makes the loop converge instead of wandering.
2. Progress is measured, not assumed. If the target count stops falling for
   several rounds the loop stops, rather than burning the whole token budget.

The failure mode this guards against
------------------------------------

The dangerous repair is the one that "succeeds" by deleting the user's work:
strip out every node and the graph validates perfectly. Three safeguards, in
increasing order of trustworthiness:

* ``REPAIR_SYSTEM`` forbids it — the cheapest guard, and the one a model can
  simply ignore.
* :func:`describe_diff` reports exactly what was added and removed, so the UI can
  highlight deletions. This cannot be ignored, and it is what the user actually
  reads before accepting.
* The ``regressed`` flag fires when the graph still fails *and* has fewer nodes
  than it started with.

Only the last two are load-bearing. User-authored text is never rewritten either
— see the ``nodes_updated`` field list in :func:`describe_diff`.
"""

from __future__ import annotations

from typing import Any

from app.ai_buildtime.gen_graph import GraphDraft, _dispatch, draft_tools
from app.runtime.ai.messages import render
# `format_issues_for_model` is re-exported rather than redefined: prompt text and
# prompt input formatting both live in `prompts.py`, and listing it in `__all__`
# keeps it discoverable from here, which is where a caller looks for it.
from app.ai_buildtime.metering import record_reply_usage
from app.ai_buildtime.prompts import build_repair_messages, format_issues_for_model
from app.graph.schema import ValidationIssue, WorkflowGraph, localize
from app.graph.validate import validate_graph

#: Consecutive rounds without the target count falling before giving up. Three
#: lets a model try a different approach after a wrong one while keeping a stuck
#: run short.
_NO_PROGRESS_PATIENCE = 3


# Sentences this module authors itself, as opposed to the ones the model writes.
# Kept in one table so a new message cannot ship in a single language — the same
# contract `translations.ts` enforces on the frontend, applied to the handful of
# strings the server produces.
_MESSAGES: dict[str, dict[str, str]] = {
    "clean": {
        "en": "This workflow already passes validation — nothing to repair.",
        "zh": "这个工作流已经通过校验，无需修复。",
    },
    "resolved": {
        # Deliberately not "blocking problem": with severity="all" the warnings
        # were targets too, so naming only errors would understate what happened.
        "en": "Fixed every reported problem.",
        "zh": "已修复全部报告的问题。",
    },
    "resolved_warnings": {
        "en": "Fixed every error. {remaining} warning(s) remain.",
        "zh": "错误已全部修复，还剩 {remaining} 个警告。",
    },
    "partial": {
        "en": "Fixed some problems; {remaining} still remain.",
        "zh": "修复了一部分问题，还剩 {remaining} 个。",
    },
    "unchanged": {
        "en": "The model did not change anything.",
        "zh": "模型没有做出任何改动。",
    },
    "stalled": {
        "en": "Stopped early: further edits stopped making progress.",
        "zh": "提前停止：继续修改已经没有进展。",
    },
    "regressed": {
        "en": "This removes {count} node(s). Check that nothing important was lost.",
        "zh": "这次改动删除了 {count} 个节点，请确认没有丢掉重要的东西。",
    },
}


def _t(key: str, lang: str, **vars: Any) -> str:
    """This module's own sentences, rendered.

    The table stays here because the sentences belong with the code that emits
    them; only the lookup-and-fall-back moved out, to `app/runtime/ai/messages.py`.
    """
    return render(_MESSAGES, key, lang, **vars)


# ---------------------------------------------------------------------------
# Targeting
# ---------------------------------------------------------------------------


def target_issues(
    graph: WorkflowGraph, severity: str = "error"
) -> list[ValidationIssue]:
    """The issues a repair run is expected to eliminate.

    ``severity="error"`` is the default because errors are what block execution
    and what the canvas badge counts. ``"all"`` also chases warnings, which is
    often counter-productive: a warning such as "this node is not connected" can
    be describing a deliberate half-finished sketch, and "fixing" it would mean
    inventing structure the user never asked for.
    """
    report = validate_graph(graph)
    if severity == "all":
        return list(report.issues)
    return [issue for issue in report.issues if issue.severity == severity]


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def describe_diff(before: WorkflowGraph, after: WorkflowGraph) -> dict[str, Any]:
    """Summarise what a repair changed, in a form the UI can render literally.

    Comparing the two graphs rather than parsing the tool transcript is
    deliberate: the transcript records what the model *tried*, including mutations
    that failed. The difference between the graphs is what actually happened.

    Node **ids** establish identity, so a rename shows up as an update rather than
    a delete plus an add. That matters here — the user has to be able to trust
    that "2 nodes removed" means exactly two nodes disappeared.
    """
    before_nodes = {node.id: node for node in before.nodes}
    after_nodes = {node.id: node for node in after.nodes}

    added = [node for node_id, node in after_nodes.items() if node_id not in before_nodes]
    removed = [node for node_id, node in before_nodes.items() if node_id not in after_nodes]

    updated: list[dict[str, Any]] = []
    for node_id, old in before_nodes.items():
        new = after_nodes.get(node_id)
        if new is None:
            continue
        fields = [
            field
            for field in ("label", "description", "config", "trusted")
            if getattr(old, field) != getattr(new, field)
        ]
        if fields:
            updated.append(
                {
                    "node_id": node_id,
                    "label": new.label or node_id,
                    "fields": fields,
                }
            )

    before_edges = {edge.id for edge in before.edges}
    after_edges = {edge.id for edge in after.edges}
    edges_added = [
        {"source": e.source, "target": e.target, "target_handle": e.target_handle}
        for e in after.edges
        if e.id not in before_edges
    ]
    edges_removed = [
        {"source": e.source, "target": e.target, "target_handle": e.target_handle}
        for e in before.edges
        if e.id not in after_edges
    ]

    return {
        "nodes_added": [
            {"node_id": n.id, "type": n.type, "label": n.label or n.id} for n in added
        ],
        "nodes_removed": [
            {"node_id": n.id, "type": n.type, "label": n.label or n.id} for n in removed
        ],
        "nodes_updated": updated,
        "edges_added": edges_added,
        "edges_removed": edges_removed,
        "node_delta": len(after_nodes) - len(before_nodes),
        "edge_delta": len(after.edges) - len(before.edges),
    }


# ---------------------------------------------------------------------------
# Repair loop
# ---------------------------------------------------------------------------


def _validation_payload(graph: WorkflowGraph, lang: str) -> dict[str, Any]:
    """The report as the *client* should see it — issue messages localised."""
    return localize(validate_graph(graph).model_dump(mode="json"), lang)


def _clean_result(graph: WorkflowGraph, lang: str) -> dict[str, Any]:
    """The "nothing to do" response.

    Returning here instead of calling the model is not merely an optimisation: a
    repair run on a healthy graph would be pure risk, and the UI would offer an
    "AI changed things" diff that changed nothing.
    """
    return {
        "graph": graph.model_dump(mode="json"),
        "validation": _validation_payload(graph, lang),
        "diff": describe_diff(graph, graph),
        "transcript": [],
        "resolved": True,
        "changed": False,
        "attempts": 0,
        "stalled": False,
        "summary": _t("clean", lang),
        "warnings": [],
        "regressed": False,
        "remaining": 0,
    }


async def repair_graph(
    graph: WorkflowGraph,
    *,
    severity: str = "error",
    lang: str = "en",
    instruction: str = "",
    max_iterations: int | None = None,
) -> dict[str, Any]:
    """Attempt to make ``graph`` pass validation, and report what happened.

    Raises ``AINotConfigured`` when no model is available. Never mutates
    ``graph`` — the draft is a copy, and the caller decides whether to apply the
    returned graph. That is the same contract as ``generate_graph``, and the
    reason an unhelpful repair cannot destroy work.
    """
    from app.ai_buildtime.gen_node import AINotConfigured
    from app.settings import get_settings

    settings = get_settings()
    if not settings.ai_enabled:
        raise AINotConfigured("AI features are disabled. Set AI_ENABLED=true in .env.")

    initial = target_issues(graph, severity)
    if not initial:
        return _clean_result(graph, lang)

    budget = max_iterations or settings.ai_max_tool_iterations

    draft = GraphDraft(graph)
    model = _build_model(settings)
    model = model.bind_tools(draft_tools())

    messages: list[Any] = build_repair_messages(
        graph, initial, lang=lang, instruction=instruction
    )
    transcript: list[dict[str, Any]] = []
    attempts = 0
    remaining = len(initial)
    no_progress_rounds = 0
    stalled = False

    for _step in range(budget):
        response = await model.ainvoke(messages)
        record_reply_usage(response)
        messages.append(response)
        attempts += 1

        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            # The model stopped mutating. Whatever it intended is already applied
            # to the draft, so the loop ends rather than spinning.
            break

        finished = False
        for call in calls:
            name = str(call.get("name") or "")
            args = call.get("args") or {}
            result = _dispatch(draft, name, args)
            transcript.append({"tool": name, "arguments": args, "result": result})
            messages.append(_tool_message(call, result))
            if name == "finish":
                finished = True

        # Re-measure. This is the heart of the difference from `generate_graph`:
        # what the model is told next is the *remaining target list*, so progress
        # is fed back rather than assumed.
        current = target_issues(draft.to_graph(name=graph.name), severity)
        if len(current) < remaining:
            no_progress_rounds = 0
        else:
            no_progress_rounds += 1
        remaining = len(current)
        messages.append(_status_message(current, finished))

        if remaining == 0:
            break

        if no_progress_rounds >= _NO_PROGRESS_PATIENCE:
            stalled = True
            break

    return build_repair_result(
        graph,
        draft.to_graph(name=graph.name),
        severity=severity,
        lang=lang,
        attempts=attempts,
        stalled=stalled,
        transcript=transcript,
    )


def build_repair_result(
    before: WorkflowGraph,
    repaired: WorkflowGraph,
    *,
    severity: str = "error",
    lang: str = "en",
    attempts: int = 0,
    stalled: bool = False,
    transcript: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Turn a before/after pair into the response payload.

    Separated from the loop above on purpose. Everything that decides whether the
    user should trust a repair — identity preservation, the diff, the
    ``regressed`` flag, the summary — is computed here, from two graphs and
    nothing else. That makes the safety logic testable without a model, which
    matters because these are exactly the rules nobody notices are broken until a
    repair eats their work.
    """
    # Identity and name are preserved deliberately: repair edits an existing
    # workflow, and a fresh id would save a second copy rather than updating this
    # one. `GraphDraft.to_graph` mints a new one because generation genuinely is a
    # new workflow; repairing is not, and it must not rename the user's work
    # either.
    repaired.id = before.id
    repaired.name = before.name

    report = validate_graph(repaired)
    remaining_issues = [
        issue
        for issue in report.issues
        if severity == "all" or issue.severity == severity
    ]
    diff = describe_diff(before, repaired)
    changed = bool(
        diff["nodes_added"]
        or diff["nodes_removed"]
        or diff["nodes_updated"]
        or diff["edges_added"]
        or diff["edges_removed"]
    )
    resolved = not remaining_issues

    warnings: list[str] = []
    # Fewer nodes than we started with *and* still broken. Either the model gave
    # up partway through a legitimately destructive fix, or it did not understand
    # the graph and deleted its way into a corner. Both deserve a look before the
    # user accepts; neither is provably wrong, so this warns rather than blocks.
    regressed = bool(not resolved and diff["node_delta"] < 0)
    if regressed:
        warnings.append(_t("regressed", lang, count=-diff["node_delta"]))

    if resolved and not changed:
        summary = _t("clean", lang)
    elif resolved and report.issues:
        # Fixed every error, warnings remain. Worth saying explicitly: the badge
        # does not disappear, and the user would otherwise read that as failure.
        summary = _t("resolved_warnings", lang, remaining=len(report.issues))
    elif resolved:
        summary = _t("resolved", lang)
    elif not changed:
        summary = _t("unchanged", lang)
    elif stalled:
        summary = _t("stalled", lang)
    else:
        summary = _t("partial", lang, remaining=len(remaining_issues))

    return {
        "graph": repaired.model_dump(mode="json"),
        "validation": localize(report.model_dump(mode="json"), lang),
        "diff": diff,
        "transcript": transcript or [],
        "resolved": resolved,
        "changed": changed,
        "attempts": attempts,
        "stalled": stalled,
        "summary": summary,
        "warnings": warnings,
        "regressed": regressed,
        "remaining": len(remaining_issues),
    }


def _build_model(settings: Any) -> Any:
    """A deterministic, non-streaming model — repairs should be reproducible."""
    from app.ai_buildtime.gen_node import AINotConfigured
    from app.runtime.ai.provider import build_chat_model

    model = build_chat_model({"temperature": 0.0}, settings=settings, streaming=False)
    if getattr(model, "model_name", "") == "mock-model":
        raise AINotConfigured(
            "No LLM API key is configured, so workflow repair is unavailable. "
            "Set LLM_API_KEY in .env and restart the backend."
        )
    return model


def _tool_message(call: dict[str, Any], result: str) -> Any:
    """Wrap a mutation result as the ``ToolMessage`` the model expects back."""
    from langchain_core.messages import ToolMessage

    return ToolMessage(
        content=str(result)[:4000],
        tool_call_id=str(call.get("id") or f"call_{call.get('name')}"),
    )


def _status_message(remaining: list[ValidationIssue], finished: bool) -> Any:
    """Tell the model what is *still* wrong, after every round of edits.

    Without this the model only sees the outcome of its own last call and has to
    hold the rest of the error list in mind from the opening prompt — which it
    does not do reliably once the conversation grows.
    """
    from langchain_core.messages import HumanMessage

    if not remaining:
        return HumanMessage(
            content="Validation now passes. Call `finish` if you have not already."
        )

    header = (
        "You called `finish`, but validation still fails. Keep working."
        if finished
        else "Validation still fails."
    )
    return HumanMessage(
        content=(
            f"{header} {len(remaining)} problem(s) remain:\n\n"
            f"{format_issues_for_model(remaining)}\n\n"
            "Fix these. Do not delete nodes merely to silence the validator, and do "
            "not rewrite the user's labels or descriptions."
        )
    )


__all__ = [
    "build_repair_result",
    "describe_diff",
    "format_issues_for_model",
    "repair_graph",
    "target_issues",
]
