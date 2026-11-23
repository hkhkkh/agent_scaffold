"""Graph validation.

Runs before compilation and before saving. Every rule here exists because it
catches a mistake that would otherwise surface as a confusing runtime error —
or worse, as a workflow that appears to work but silently does nothing.

Errors block execution; warnings are advisory and shown in the UI.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from app.graph.schema import (
    EdgeSpec,
    ValidationIssue,
    ValidationReport,
    WorkflowGraph,
    ports_compatible,
)
from app.nodes import (
    effective_inputs,
    effective_outputs,
    get_node_type_spec,
    has_node_type,
)
from app.runtime.template import find_placeholders
from app.nodes.control import LOOP_BRANCH_KEY
from app.nodes.condition import BRANCH_KEY
from app.settings import get_settings
from app.graph.subgraphs import (
    SUBGRAPH_TYPE,
    chain_has_cycle,
    digest_matches,
    load_subgraph,
    reference_of,
)


class _Issues:
    """Collector for validation findings.

    Both helpers take the English sentence and its Chinese counterpart together
    so a rule can never silently ship in one language only; :func:`localize`
    picks the right one when the report leaves the API.
    """

    def __init__(self) -> None:
        self.items: list[ValidationIssue] = []

    def error(self, code: str, message: str, message_zh: str = "", **kw) -> None:
        self.items.append(
            ValidationIssue(
                severity="error", code=code, message=message, message_zh=message_zh, **kw
            )
        )

    def warn(self, code: str, message: str, message_zh: str = "", **kw) -> None:
        self.items.append(
            ValidationIssue(
                severity="warning",
                code=code,
                message=message,
                message_zh=message_zh,
                **kw,
            )
        )


def _zh_label(obj: Any) -> str:
    """Chinese label for a port or param, falling back to its English one.

    A message like "needs a value for 'Prompt'" names a *field the user sees in
    the Inspector*. Quoting the English label inside an otherwise Chinese
    sentence points at a control that is not labelled that way on screen, so
    every catalogue name interpolated into a ``*_zh`` message goes through here.
    User-authored labels (a node's own name) are not touched — those are the
    user's words in whatever language they typed.
    """
    override = getattr(obj, "label_zh", "")
    return override if isinstance(override, str) and override else str(obj.label)


def validate_graph(graph: WorkflowGraph) -> ValidationReport:
    issues = _Issues()
    nodes = {n.id: n for n in graph.nodes}

    _check_unknown_types(graph, issues)
    _check_duplicate_ids(graph, issues)
    _check_graph_size(graph, issues)
    _check_code_nodes(graph, issues)
    _check_edges_resolve(graph, nodes, issues)
    _check_port_types(graph, nodes, issues)
    _check_entry_and_terminal(graph, issues)
    _check_connectivity(graph, nodes, issues)
    _check_required_params(graph, issues)
    _check_trust(graph, issues)
    _check_condition_wiring(graph, issues)
    _check_loops(graph, issues)
    _check_containers(graph, issues)
    _check_subflow_refs(graph, issues)
    _check_template_refs(graph, nodes, issues)

    return ValidationReport(ok=not any(i.severity == "error" for i in issues.items), issues=issues.items)


def _check_graph_size(graph: WorkflowGraph, issues: _Issues) -> None:
    """Optional deployment caps, checked before a large graph reaches a worker."""
    settings = get_settings()
    if settings.max_nodes_per_workflow and len(graph.nodes) > settings.max_nodes_per_workflow:
        issues.error(
            "graph_too_many_nodes",
            f"This deployment allows at most {settings.max_nodes_per_workflow} nodes; "
            f"the graph has {len(graph.nodes)}.",
            f"此部署最多允许 {settings.max_nodes_per_workflow} 个节点；当前图有 "
            f"{len(graph.nodes)} 个。",
        )
    if settings.max_edges_per_workflow and len(graph.edges) > settings.max_edges_per_workflow:
        issues.error(
            "graph_too_many_edges",
            f"This deployment allows at most {settings.max_edges_per_workflow} edges; "
            f"the graph has {len(graph.edges)}.",
            f"此部署最多允许 {settings.max_edges_per_workflow} 条连线；当前图有 "
            f"{len(graph.edges)} 条。",
        )


def _check_code_nodes(graph: WorkflowGraph, issues: _Issues) -> None:
    """Warn, rather than error, when Code is disabled.

    The flagship workflow contains Code nodes. A public visitor must be able to
    open it and read it; the refusal belongs at the save/run boundary, not in the
    canvas's problem list. The warning still tells the user why Run will refuse.
    """
    settings = get_settings()
    if not settings.public_demo and settings.code_node_enabled:
        return
    for node in graph.nodes:
        if node.type != "code":
            continue
        issues.warn(
            "code_node_disabled",
            f"'{node.label or node.id}' is a Code node. Code execution is disabled "
            "in the public demo, so this workflow is display-only.",
            f"「{node.label or node.id}」是 Code 节点。公开演示已禁用代码执行，"
            "此工作流仅可查看。",
            node_id=node.id,
        )


def _check_subflow_refs(graph: WorkflowGraph, issues: _Issues) -> None:
    """Sub-flow nodes must point at something readable, and not at themselves.

    The compiler refuses these too — this rule exists so the canvas says so
    *before* a run, next to the node, rather than as a 422 after pressing Run.
    """
    for node in graph.nodes:
        if node.type != SUBGRAPH_TYPE:
            continue
        ref = reference_of(node)
        if not ref:
            issues.error(
                "subflow_missing_ref",
                f"'{node.label or node.id}' is a sub-flow but no workflow is selected.",
                f"「{node.label or node.id}」是子流程，但还没有选择任何工作流。",
                node_id=node.id,
            )
            continue
        child = load_subgraph(ref)
        if child is None:
            issues.error(
                "subflow_missing_workflow",
                f"'{node.label or node.id}' refers to workflow '{ref}', which does "
                f"not exist. It may have been deleted.",
                f"「{node.label or node.id}」引用的工作流「{ref}」不存在，可能已被删除。",
                node_id=node.id,
            )
            continue
        if not digest_matches(node, child):
            issues.error(
                "subflow_changed",
                f"'{node.label or node.id}' was saved against an older version of "
                f"'{ref}'. Re-save this workflow to accept the change, or you may "
                f"get a different result than when you last ran it.",
                f"「{node.label or node.id}」保存时引用的「{ref}」是旧版本，已被改动。"
                f"请重新保存本工作流以接受这次改动，否则运行结果会和上次不一样。",
                node_id=node.id,
            )

    cycle = chain_has_cycle(graph)
    if cycle:
        chain = " -> ".join(cycle)
        # f-strings (not `+` concatenation) because `scripts/check_i18n.py` reads
        # these calls statically and only recognises a Chinese message it can see
        # as a literal or f-string. A concatenation reads as "no Chinese message",
        # which is how this rule was flagged the first time.
        issues.error(
            "subflow_cycle",
            f"Sub-flow reference loop: {chain}.",
            f"子流程循环引用：{chain}。",
        )


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _check_unknown_types(graph: WorkflowGraph, issues: _Issues) -> None:
    for node in graph.nodes:
        if node.type == "group":
            continue
        if not has_node_type(node.type):
            issues.error(
                "unknown_node_type",
                f"Node type '{node.type}' is not registered. It may have been removed "
                f"or renamed.",
                f"节点类型「{node.type}」未注册，可能已被移除或改名。",
                node_id=node.id,
            )


def _check_duplicate_ids(graph: WorkflowGraph, issues: _Issues) -> None:
    seen: set[str] = set()
    for node in graph.nodes:
        if node.id in seen:
            issues.error(
                "duplicate_node_id",
                f"Duplicate node id '{node.id}'.",
                f"节点 ID「{node.id}」重复。",
                node_id=node.id,
            )
        seen.add(node.id)

    seen_edges: set[str] = set()
    for edge in graph.edges:
        if edge.id in seen_edges:
            issues.error(
                "duplicate_edge_id",
                f"Duplicate edge id '{edge.id}'.",
                f"连线 ID「{edge.id}」重复。",
                edge_id=edge.id,
            )
        seen_edges.add(edge.id)


def _check_edges_resolve(
    graph: WorkflowGraph, nodes: dict[str, object], issues: _Issues
) -> None:
    for edge in graph.edges:
        if edge.source not in nodes:
            issues.error(
                "dangling_edge",
                f"Connection starts at '{edge.source}', which does not exist.",
                f"连线起点「{edge.source}」不存在。",
                edge_id=edge.id,
            )
            continue
        if edge.target not in nodes:
            issues.error(
                "dangling_edge",
                f"Connection ends at '{edge.target}', which does not exist.",
                f"连线终点「{edge.target}」不存在。",
                edge_id=edge.id,
            )
            continue
        if edge.source == edge.target:
            issues.warn(
                "self_loop",
                "A node is connected to itself. Use a Loop node instead — a direct "
                "self-connection has no exit condition.",
                "节点连到了自己。请改用「循环」节点——自环没有退出条件。",
                edge_id=edge.id,
            )

        source_node = nodes[edge.source]
        target_node = nodes[edge.target]
        _check_handles(edge, source_node, target_node, issues)


def _check_handles(edge, source_node, target_node, issues: _Issues) -> None:
    out_ids = {p.id for p in effective_outputs(source_node)}
    in_ids = {p.id for p in effective_inputs(target_node)}

    if edge.source_handle and out_ids and edge.source_handle not in out_ids:
        issues.error(
            "unknown_source_handle",
            f"'{source_node.label or source_node.id}' has no output named "
            f"'{edge.source_handle}'. Available: {', '.join(sorted(out_ids)) or 'none'}.",
            f"「{source_node.label or source_node.id}」没有名为「{edge.source_handle}」"
            f"的输出。可用：{', '.join(sorted(out_ids)) or '无'}。",
            edge_id=edge.id,
        )
    if edge.target_handle and in_ids and edge.target_handle not in in_ids:
        issues.error(
            "unknown_target_handle",
            f"'{target_node.label or target_node.id}' has no input named "
            f"'{edge.target_handle}'. Available: {', '.join(sorted(in_ids)) or 'none'}.",
            f"「{target_node.label or target_node.id}」没有名为「{edge.target_handle}」"
            f"的输入。可用：{', '.join(sorted(in_ids)) or '无'}。",
            edge_id=edge.id,
        )


def _check_port_types(
    graph: WorkflowGraph, nodes: dict[str, object], issues: _Issues
) -> None:
    """Reject connections whose data types cannot flow (the Langflow rule)."""
    for edge in graph.edges:
        source_node = nodes.get(edge.source)
        target_node = nodes.get(edge.target)
        if source_node is None or target_node is None:
            continue
        if getattr(source_node, "type", None) == "group" or getattr(target_node, "type", None) == "group":
            continue

        out_port = next(
            (p for p in effective_outputs(source_node) if p.id == edge.source_handle),
            None,
        )
        in_port = next(
            (p for p in effective_inputs(target_node) if p.id == edge.target_handle),
            None,
        )
        if out_port is None or in_port is None:
            continue

        if not ports_compatible(out_port.type, in_port.type):
            issues.error(
                "incompatible_port_types",
                f"Cannot connect {out_port.type.value} output "
                f"'{source_node.label or source_node.id} · {out_port.label}' to "
                f"{in_port.type.value} input "
                f"'{target_node.label or target_node.id} · {in_port.label}'.",
                f"无法把 {out_port.type.value} 输出「{source_node.label or source_node.id}"
                f" · {_zh_label(out_port)}」连接到 {in_port.type.value} 输入"
                f"「{target_node.label or target_node.id} · {_zh_label(in_port)}」。",
                edge_id=edge.id,
            )


def _check_entry_and_terminal(graph: WorkflowGraph, issues: _Issues) -> None:
    if not graph.executable_nodes():
        return

    entries = [n for n in graph.nodes if n.type == "start"]
    terminals = [n for n in graph.nodes if n.type == "end"]

    if not entries:
        issues.error(
            "missing_entry",
            "The workflow needs a Start node.",
            "工作流需要一个「开始」节点。",
        )
    elif len(entries) > 1:
        issues.error(
            "multiple_entries",
            f"Found {len(entries)} Start nodes. A workflow may have only one.",
            f"找到 {len(entries)} 个「开始」节点，一个工作流只能有一个。",
            node_id=entries[1].id,
        )

    if not terminals:
        issues.warn(
            "missing_terminal",
            "There is no End node, so the run will produce no result.",
            "没有「结束」节点，运行不会产出结果。",
        )


def _check_connectivity(
    graph: WorkflowGraph, nodes: dict[str, object], issues: _Issues
) -> None:
    executable = {n.id for n in graph.executable_nodes()}
    if not executable:
        return

    outgoing: dict[str, list[EdgeSpec]] = defaultdict(list)
    incoming: dict[str, list[EdgeSpec]] = defaultdict(list)
    for edge in graph.edges:
        outgoing[edge.source].append(edge)
        incoming[edge.target].append(edge)

    # 1. Orphaned nodes: nothing in, nothing out (and not the entry point).
    for node in graph.executable_nodes():
        if node.type == "start":
            continue
        if not outgoing.get(node.id) and not incoming.get(node.id):
            issues.warn(
                "orphan_node",
                f"'{node.label or node.id}' is not connected to anything and will "
                f"never run.",
                f"「{node.label or node.id}」没有连接任何东西，永远不会运行。",
                node_id=node.id,
            )

    # 2. Unreachable from the entry point.
    entry = next((n for n in graph.nodes if n.type == "start"), None)
    if entry:
        reachable: set[str] = set()
        queue = deque([entry.id])
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            for edge in outgoing.get(current, []):
                queue.append(edge.target)

        for node_id in sorted(executable - reachable):
            node = nodes[node_id]
            issues.warn(
                "unreachable_node",
                f"'{node.label or node_id}' cannot be reached from Start.",
                f"从「开始」无法到达「{node.label or node_id}」。",
                node_id=node_id,
            )

    # 3. Required inputs with nothing wired in.
    for node in graph.executable_nodes():
        if node.type in ("start", "human_input"):
            continue
        wired = {e.target_handle for e in incoming.get(node.id, [])}
        for port in effective_inputs(node):
            if port.required and port.id not in wired and None not in wired:
                issues.error(
                    "missing_required_input",
                    f"'{node.label or node.id}' requires an input on "
                    f"'{port.label}' but nothing is connected.",
                    f"「{node.label or node.id}」的输入「{_zh_label(port)}」是必填的，"
                    f"但没有连接任何东西。",
                    node_id=node.id,
                )


def _check_required_params(graph: WorkflowGraph, issues: _Issues) -> None:
    from app.nodes import get_node_type_spec

    for node in graph.executable_nodes():
        if not has_node_type(node.type):
            continue
        spec = get_node_type_spec(node.type)
        for param in spec.params:
            if not param.required:
                continue

            if param.name in node.config:
                # Explicitly set. An empty value is a deliberate clear, so the
                # declared default must not silently rescue it.
                if node.config[param.name] in (None, "", [], {}):
                    issues.error(
                        "missing_required_param",
                        f"'{node.label or node.id}' needs a value for '{param.label}'.",
                        f"「{node.label or node.id}」的参数「{_zh_label(param)}」需要填一个值。",
                        node_id=node.id,
                    )
            else:
                # Never set. Acceptable only when the type supplies a usable default.
                default = getattr(param, "default", None)
                if default in (None, "", [], {}):
                    issues.error(
                        "missing_required_param",
                        f"'{node.label or node.id}' needs a value for '{param.label}'.",
                        f"「{node.label or node.id}」的参数「{_zh_label(param)}」需要填一个值。",
                        node_id=node.id,
                    )


def _check_trust(graph: WorkflowGraph, issues: _Issues) -> None:
    from app.nodes import get_node_type_spec

    for node in graph.executable_nodes():
        if not has_node_type(node.type):
            continue
        spec = get_node_type_spec(node.type)
        if spec.requires_trust and not node.trusted:
            issues.error(
                "untrusted_node",
                f"'{node.label or node.id}' runs code you supplied. Tick "
                f"'Trusted' in the Inspector to allow it to execute.",
                f"「{node.label or node.id}」会执行你写的代码。请在右侧面板"
                f"勾选「受信任」以允许它运行。",
                node_id=node.id,
            )


def _check_condition_wiring(graph: WorkflowGraph, issues: _Issues) -> None:
    """A Condition with no outgoing edge is almost always a mistake."""
    for node in graph.executable_nodes():
        if node.type not in ("condition", "loop"):
            continue
        outgoing = graph.outgoing(node.id)
        if not outgoing:
            issues.warn(
                "branch_without_targets",
                f"'{node.label or node.id}' has no outgoing connections, so every "
                f"branch is a dead end.",
                f"「{node.label or node.id}」没有任何出边，所有分支都是死路。",
                node_id=node.id,
            )
            continue

        handles = {e.source_handle for e in outgoing}
        if node.type == "condition":
            from app.nodes import effective_outputs

            declared = {p.id for p in effective_outputs(node)}
            unused = declared - handles
            if unused:
                issues.warn(
                    "unused_branch",
                    f"'{node.label or node.id}' has {len(unused)} branch(es) with no "
                    f"target: {', '.join(sorted(unused))}.",
                    f"「{node.label or node.id}」有 {len(unused)} 个分支没有目标："
                    f"{', '.join(sorted(unused))}。",
                    node_id=node.id,
                )
        elif handles and not handles & {"continue", "done"}:
            issues.warn(
                "loop_without_targets",
                f"'{node.label or node.id}' should have its 'Continue' or 'Done' "
                f"output connected.",
                f"「{node.label or node.id}」的「Continue」或「Done」输出应该连接上去。",
                node_id=node.id,
            )


def _check_loops(graph: WorkflowGraph, issues: _Issues) -> None:
    """Detect cycles and confirm each one passes through a bounded Loop node.

    Implemented with Tarjan's strongly-connected-component algorithm rather than
    a DFS back-edge hunt. The naive version reports a different "offending" node
    depending on where the traversal happens to start, which produces baffling
    messages. An SCC is order-independent: a component with more than one node
    *is* a cycle, and it either contains a Loop node or it does not.
    """
    executable = {n.id for n in graph.executable_nodes()}
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.source in executable and edge.target in executable:
            adjacency[edge.source].append(edge.target)

    loop_nodes = {n.id for n in graph.nodes if n.type == "loop"}
    node_index = {n.id: n for n in graph.nodes}

    for component in _strongly_connected_components(executable, adjacency):
        is_cycle = len(component) > 1
        if not is_cycle:
            sole = next(iter(component))
            if sole in adjacency.get(sole, []):
                is_cycle = True  # a node connected to itself
        if not is_cycle:
            continue

        if loop_nodes & component:
            continue

        names = ", ".join(
            sorted(node_index[n].label or n for n in component if n in node_index)
        )
        issues.error(
            "unbounded_cycle",
            f"These nodes form a loop with no stopping condition: {names}. "
            f"Add a Loop node inside the cycle to bound it, otherwise the engine "
            f"will abort the run once it hits the recursion limit.",
            f"这些节点构成了一个没有停止条件的环：{names}。请在环内加一个"
            f"「循环」节点来限制它，否则引擎会在达到递归上限时中止运行。",
            node_id=sorted(component)[0],
        )


def _strongly_connected_components(
    nodes: set[str], adjacency: dict[str, list[str]]
) -> list[set[str]]:
    """Tarjan's SCC algorithm, iterative to avoid deep-recursion limits."""
    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[set[str]] = []

    for root in nodes:
        if root in indices:
            continue

        # Each work item is (node, iterator position over its neighbours).
        work: list[tuple[str, int]] = [(root, 0)]
        indices[root] = lowlink[root] = index_counter
        index_counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            node, position = work[-1]
            neighbours = adjacency.get(node, [])

            if position < len(neighbours):
                work[-1] = (node, position + 1)
                neighbour = neighbours[position]
                if neighbour not in indices:
                    indices[neighbour] = lowlink[neighbour] = index_counter
                    index_counter += 1
                    stack.append(neighbour)
                    on_stack.add(neighbour)
                    work.append((neighbour, 0))
                elif neighbour in on_stack:
                    lowlink[node] = min(lowlink[node], indices[neighbour])
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])

            if lowlink[node] == indices[node]:
                component: set[str] = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                components.append(component)

    return components


def _check_containers(graph: WorkflowGraph, issues: _Issues) -> None:
    """React Flow requires a parent to appear before its children in the array."""
    index_of = {node.id: i for i, node in enumerate(graph.nodes)}
    for node in graph.nodes:
        parent_id = node.ui.parent_id
        if not parent_id:
            continue
        if parent_id not in index_of:
            issues.error(
                "missing_parent",
                f"'{node.label or node.id}' is nested inside '{parent_id}', which "
                f"does not exist.",
                f"「{node.label or node.id}」嵌套在「{parent_id}」里，"
                f"但这个父节点不存在。",
                node_id=node.id,
            )
            continue
        if index_of[parent_id] > index_of[node.id]:
            issues.warn(
                "parent_order",
                f"'{node.label or node.id}' is nested inside "
                f"'{parent_id}', which is declared after it. Reordering on save "
                f"avoids rendering glitches.",
                f"「{node.label or node.id}」嵌套在「{parent_id}」里，"
                f"但父节点声明在它之后。保存时重排可以避免渲染异常。",
                node_id=node.id,
            )


# ---------------------------------------------------------------------------
# Template references
# ---------------------------------------------------------------------------

#: Top-level keys a Loop node writes. `out` and `value` are the forwarded
#: value; `iteration` is the pass counter. It is deliberately *not* nested under
#: `out`, which is the mistake this rule exists to catch.
_LOOP_TOP_LEVEL = {"iteration", "continue", "done", "out", "value"}


def _walk_template_strings(
    value: Any,
    skip: set[str],
    prefix: tuple[str, ...] = (),
):
    """Yield (config field path, string) for every string that may hold a template."""
    if isinstance(value, str):
        yield ".".join(prefix), value
        return
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            # Only the node's own top-level `code` field is skipped: a Code
            # node's Python may legitimately contain `{{` (it is building a
            # prompt, for example), and scanning it would be a false positive.
            if not prefix and name in skip:
                continue
            yield from _walk_template_strings(item, skip, prefix + (name,))
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_template_strings(item, skip, prefix + (str(index),))


def _check_template_refs(graph: WorkflowGraph, nodes: dict[str, Any], issues: _Issues) -> None:
    """`{{ vars.<node>... }}` must name a node and an output handle that exist.

    The renderer treats an unknown path as an empty string, so a typo compiles
    and runs — it just silently drops a value. That made this class of mistake
    invisible to the generator's self-correction loop, which only sees the
    validator's report.
    """
    reported: set[tuple[str, str]] = set()

    for node in graph.nodes:
        if not has_node_type(node.type):
            continue
        spec = get_node_type_spec(node.type)
        # Skip executable source only. A Prompt node's `template` is also a
        # `code` param, but its language is jinja2 and that is exactly what we
        # came here to scan.
        code_fields = {
            param.name
            for param in spec.params
            if getattr(param, "type", None) == "code"
            and getattr(param, "language", None) not in {"jinja2", "jinja"}
        }
        for field, text in _walk_template_strings(node.config, code_fields):
            for path in find_placeholders(text):
                key = (node.id, path)
                if key in reported:
                    continue
                reported.add(key)
                _check_template_path(node, path, field, nodes, issues)


def _check_template_path(
    node: Any,
    path: str,
    field: str,
    nodes: dict[str, Any],
    issues: _Issues,
) -> None:
    # `inputs.*`, `params.*` and bare state names are runtime-dependent — the
    # Start node's schema is a sample, and run inputs can add keys — so only
    # `vars.*` paths are statically checkable.
    if not path.startswith("vars."):
        return
    parts = path.split(".")
    if len(parts) < 2 or not parts[1]:
        return

    owner = node.label or node.id
    target_id = parts[1]
    target = nodes.get(target_id)
    placeholder = "{{ " + path + " }}"

    if target is None:
        issues.error(
            "unknown_template_node",
            f"'{owner}' uses {placeholder} in '{field}', but no node "
            f"'{target_id}' exists.",
            f"「{owner}」的「{field}」引用了 {placeholder}，但节点「{target_id}」不存在。",
            node_id=node.id,
        )
        return

    if target.type == "loop":
        if len(parts) < 3:
            return
        top = parts[2]
        if top not in _LOOP_TOP_LEVEL:
            available = ", ".join(sorted(_LOOP_TOP_LEVEL))
            issues.error(
                "unknown_template_field",
                f"'{owner}' reads {placeholder}, but a Loop node exposes "
                f"{available} at the top level.",
                f"「{owner}」读取 {placeholder}，但 Loop 节点顶层只有 {available}。",
                node_id=node.id,
            )
            return
        if top == "out" and len(parts) >= 4 and parts[3] in {"iteration", "continue", "done"}:
            issues.error(
                "loop_iteration_misplaced",
                f"'{owner}' reads {placeholder}, but the pass counter is not "
                f"inside 'out'. Use {{{{ vars.{target_id}.iteration }}}}.",
                f"「{owner}」读取 {placeholder}，但轮次不在 'out' 里面。"
                f"请用 {{{{ vars.{target_id}.iteration }}}}。",
                node_id=node.id,
            )
        return

    if len(parts) < 3:
        return

    handle = parts[2]
    if not has_node_type(target.type):
        return
    available = {port.id for port in effective_outputs(target)}
    if handle not in available:
        names = ", ".join(sorted(available)) or "none"
        issues.error(
            "unknown_template_handle",
            f"'{owner}' reads {placeholder}, but node '{target_id}' has no "
            f"output '{handle}'. Available: {names}.",
            f"「{owner}」读取 {placeholder}，但节点「{target_id}」没有输出「{handle}」。"
            f"可用：{names}。",
            node_id=node.id,
        )


__all__ = ["validate_graph"]
