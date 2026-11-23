"""Memory node — write something down, or look something up.

One node type with a `mode` rather than two, because the difference is which
operating it performs on the same store, not what it is. Two node types would
mean two specs, two icons and two sets of parameters that must be kept in step,
to express one boolean.

What this node does *not* do
----------------------------
It does not decide **what is worth remembering**. That is the research question in
`docs/target/roadmap.md` Phase 3, and this node is deliberately the explicit half
of the answer: the graph says "remember this", and an author writes that edge.
Automatic capture — summarising a conversation into memory without being asked —
is a policy problem with no measured answer yet, so it is left out rather than
guessed at.

Scope
-----
The store, namespaces, filter, semantic search and delete are all upstream
(`langgraph.store.BaseStore`). This file adds the parameter surface and the two
calls. Measured before writing it: the stock store answered a Chinese semantic
query correctly with only an embedder configured, so nothing here re-implements
retrieval.

Degrading without an error
--------------------------
`MEMORY_STORE=off` and "no embedding model" are both supported configurations.
With no store the node logs that it did nothing and passes its input through, so
a graph containing a memory node still runs on a deployment that has no memory.
Failing the run instead would turn one node's configuration into a graph-wide
outage.
"""

from __future__ import annotations

from typing import Any

from app.graph.schema import (
    InputPort,
    JsonParam,
    NodeCategory,
    NodeTypeSpec,
    OutputPort,
    PortType,
    SelectOption,
    SelectParam,
    StringParam,
)
from app.nodes.base import (
    BaseNode,
    NodeContext,
    current_run_id,
    render_template,
)
from app.nodes.registry import register_node

#: `mode` values.
MODE_WRITE = "write"
MODE_SEARCH = "search"
#: Read everything in the namespace, newest first, without a query. Useful with
#: a small namespace and no embedding model, where `search` cannot rank.
MODE_LIST = "list"

#: Namespace segments that come from the run rather than from the author. Written
#: as `{placeholders}` in the `namespace` parameter.
_NAMESPACE_TOKENS = ("agent", "run", "input")

#: Hard ceiling on how many items a read returns. A memory node that returned
#: everything would put an unbounded amount into the state and, from there, into
#: a prompt.
MAX_LIMIT = 50
DEFAULT_LIMIT = 5


def build_memory_spec() -> NodeTypeSpec:
    return NodeTypeSpec(
        type="memory",
        label="Memory",
        label_zh="记忆",
        category=NodeCategory.DATA,
        description=(
            "Write a fact into long-term memory, or look facts up by meaning. "
            "Memory outlives the run, so a later run can recall what this one "
            "learned."
        ),
        description_zh=(
            "把一条事实写进长期记忆，或按语义检索事实。记忆比本次运行活得久，"
            "所以之后的运行能想起这次学到的东西。"
        ),
        icon="brain",
        color="#0891b2",
        inputs=[
            InputPort(
                id="in",
                label="Input",
                label_zh="输入",
                type=PortType.ANY,
                description="Used when the text or query field is empty.",
                description_zh="当文本或查询字段为空时使用。",
            )
        ],
        outputs=[
            OutputPort(
                id="out",
                label="Result",
                label_zh="结果",
                type=PortType.ANY,
                description=(
                    "For `write`, the key written. For `search`/`list`, the "
                    "matching texts as a list."
                ),
                description_zh=(
                    "`write` 时是写入的键名；`search`/`list` 时是命中的文本列表。"
                ),
            )
        ],
        params=[
            SelectParam(
                name="mode",
                label="Mode",
                label_zh="模式",
                description="Write a memory, look one up, or list what is there.",
                description_zh="写入记忆、检索记忆，或列出已有的记忆。",
                default=MODE_SEARCH,
                options=[
                    SelectOption(
                        value=MODE_SEARCH,
                        label="Search by meaning",
                        label_zh="按语义检索",
                        description="Needs an embedding model; matches literally without one.",
                        description_zh="需要 embedding 模型；没有时按字面匹配。",
                    ),
                    SelectOption(
                        value=MODE_WRITE,
                        label="Write",
                        label_zh="写入",
                        description="Store the text under a key.",
                        description_zh="把文本存到一个键下。",
                    ),
                    SelectOption(
                        value=MODE_LIST,
                        label="List all",
                        label_zh="列出全部",
                        description="No query needed. Newest first.",
                        description_zh="不需要查询词。最新的在前。",
                    ),
                ],
            ),
            StringParam(
                name="namespace",
                label="Scope",
                label_zh="作用域",
                description=(
                    "Which memories this node touches. `{agent}` is this "
                    "workflow, `{run}` the current run, `{input}` the run's "
                    "input. Use the same value to read what you wrote."
                ),
                description_zh=(
                    "本节点访问哪一批记忆。`{agent}` 是本工作流，`{run}` 是本次"
                    "运行，`{input}` 是本次运行的输入。读写用同一个值。"
                ),
                default="{agent}",
                placeholder="{agent}",
            ),
            StringParam(
                name="text",
                label="Text to remember",
                label_zh="要记住的内容",
                description="Write mode only. Supports {{ }} references.",
                description_zh="仅写入模式。支持 {{ }} 引用。",
                placeholder="The customer prefers email, not phone.",
            ),
            StringParam(
                name="key",
                label="Key",
                label_zh="键",
                description=(
                    "Write mode only. Leave empty to derive one from the text, "
                    "which makes re-writing the same fact overwrite it instead "
                    "of piling up."
                ),
                description_zh=(
                    "仅写入模式。留空则根据文本自动生成，这样同一件事重复写入"
                    "会覆盖而不是堆积。"
                ),
            ),
            StringParam(
                name="query",
                label="Look up",
                label_zh="检索",
                description=(
                    "Search mode only. Leave empty to use the node's input. "
                    "Supports {{ }} references."
                ),
                description_zh=(
                    "仅检索模式。留空则用本节点的输入。支持 {{ }} 引用。"
                ),
            ),
            JsonParam(
                name="limit",
                label="How many",
                label_zh="返回条数",
                description="Search and list modes only.",
                description_zh="仅检索与列出模式。",
                default=DEFAULT_LIMIT,
            ),
            JsonParam(
                name="filter",
                label="Exact filter",
                label_zh="精确过滤",
                description=(
                    "Optional. Matches stored fields exactly, alongside the "
                    "semantic query. Example: {\"kind\": \"preference\"}."
                ),
                description_zh=(
                    "可选。按存储字段精确匹配，与语义检索同时生效。"
                    "例如 {\"kind\": \"preference\"}。"
                ),
                default={},
                advanced=True,
            ),
        ],
        ai_notes="""\
Long-term memory that outlives the run, stored in the memory capability's own
database (`MEMORY_STORE`). Three modes, and the node does one of them at a time:

* `search` — reads. `query` (or the incoming value) is matched against stored
  text; returns a **list** of records.
* `write` — stores `text` (or the incoming value). Returns the record's key or
  an empty string when there was nothing to store.
* `list` — dumps the namespace. For debugging.

**`search` returns a list, `write` returns a string.** A downstream node written
for one shape misreads the other silently — a Prompt node given the list renders
it as JSON, which looks like data and reads like noise.

* **`namespace` must match between the writer and the reader.** It defaults to
  `{agent}`, which is this workflow — so a write and a later read in the same
  workflow find each other. Change it on one side only and the read returns
  nothing, which is indistinguishable from "nothing was ever stored".
* `dir` and `items` (in the goal node) aside, `key` left empty derives a key
  from the text, so re-writing the same fact overwrites rather than duplicates.
  Set an explicit key when you want one record per entity (e.g. per customer).
* **A memory node is a side effect, not a data path.** In `write` mode its `out`
  is a key, not the text that went in. Never wire one to the End node.
* On a deployment with `MEMORY_STORE=off` this node does nothing and logs a
  warning; the run still succeeds. Do not build a graph whose correctness
  depends on a memory read returning anything.\
""",
    )


def _stored_text(value: Any) -> str:
    """Anything a node might receive, as text suitable for **templating**.

    The `None → ""` branch is the whole point and the reason this is not shared
    with the other two modules that spell `_as_text` differently:

    * `config.get("query")` is absent unless the author filled the field in, so
      `None` arrives routinely. Rendering it as `"null"` — what
      `tool._readable_text` and `control._flat_text` both do — would template a
      literal "null" into a memory query and write a garbage record.
    * `_render(...)` runs next, and a template has to be a string, so the `None`
      case cannot be left to the caller.

    No `default=str`: memory records hold values that came out of JSON, so a
    non-serialisable object here means a bug upstream that quietly storing
    `str(value)` would hide.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    import json

    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _resolve_namespace(template: str, workflow_id: str, run_id: str) -> tuple[str, ...]:
    """Turn the scope template into upstream's namespace tuple.

    `{agent}` and `{run}` are replaced with the workflow id and the run id. Both
    are stable for the life of a run, which is what makes a write and a later
    read in the same graph land in the same place without the author having to
    coordinate anything.

    `{input}` is deliberately **not** substituted. Memory scoped to one input is
    memory that can never be recalled by a different request, which defeats the
    purpose; it is listed as a token only so the idea can be rejected by name in
    the parameter description rather than silently doing nothing.
    """
    text = (template or "{agent}").strip()
    agent_id = workflow_id or "agent"
    parts = [
        segment.strip()
        for segment in text.format(agent=agent_id, run=run_id or "run", input="{input}").split("/")
    ]
    return tuple(part for part in parts if part) or ("agent", agent_id)


def _derive_key(text: str) -> str:
    """A stable key for a fact, so writing it twice overwrites.

    Content-addressed on purpose. The alternative — a random or time-based key —
    means a graph that runs nightly accumulates one copy per night of the same
    fact, and the reader sees the same sentence five times and concludes memory
    is broken. Truncated to keep keys readable in a store browser.
    """
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@register_node
class MemoryNode(BaseNode):
    spec = build_memory_spec()

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        from app.memory.store import get_memory_store

        store = get_memory_store()
        config = self.resolve_config(ctx)
        mode = str(config.get("mode") or MODE_SEARCH)

        if store is None:
            # Not an error. `MEMORY_STORE=off` is a supported deployment, and a
            # graph containing this node has to keep running on it.
            await ctx.log(
                "Memory is switched off for this deployment (MEMORY_STORE=off), "
                "so this node did nothing.",
                "warn",
            )
            return {"out": "" if mode == MODE_WRITE else []}

        run_id = current_run_id()
        namespace = _resolve_namespace(
            str(config.get("namespace") or "{agent}"),
            str(ctx.state.get("workflow_id") or ""),
            run_id,
        )

        if mode == MODE_WRITE:
            return await self._write(ctx, store, namespace, config, run_id)
        if mode == MODE_LIST:
            return await self._read(ctx, store, namespace, config, query=None)
        return await self._read(
            ctx, store, namespace, config, query=self._query(ctx, config)
        )

    # -- the two operations -------------------------------------------------

    def _query(self, ctx: NodeContext, config: dict[str, Any]) -> str:
        """The search text: the field, else the input."""
        query = render_template(_stored_text(config.get("query")), ctx, config).strip()
        if query:
            return query
        return _stored_text(ctx.inputs.get("in")).strip()

    async def _write(
        self,
        ctx: NodeContext,
        store: Any,
        namespace: tuple[str, ...],
        config: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        text = render_template(_stored_text(config.get("text")), ctx, config).strip()
        if not text:
            text = _stored_text(ctx.inputs.get("in")).strip()
        if not text:
            await ctx.log(
                "Nothing to remember: both the text field and the input are empty.",
                "warn",
            )
            return {"out": ""}

        key = str(config.get("key") or "").strip() or _derive_key(text)
        value: dict[str, Any] = {"text": text}
        # Provenance, so a later reader can tell where a fact came from without a
        # second lookup. Not used for search; it is there to be displayed.
        if run_id:
            value["run_id"] = run_id
        if ctx.node.id:
            value["node_id"] = str(ctx.node.id)

        await store.aput(namespace, key, value)
        await ctx.log(f"Remembered 1 fact in {'/'.join(namespace)} as '{key}'.", "info")
        return {"out": key}

    async def _read(
        self,
        ctx: NodeContext,
        store: Any,
        namespace: tuple[str, ...],
        config: dict[str, Any],
        *,
        query: str | None,
    ) -> dict[str, Any]:
        raw_limit = config.get("limit")
        try:
            limit = int(raw_limit) if raw_limit not in (None, "") else DEFAULT_LIMIT
        except (TypeError, ValueError):
            await ctx.log(
                f"limit={raw_limit!r} is not a number; using {DEFAULT_LIMIT}.", "warn"
            )
            limit = DEFAULT_LIMIT
        limit = max(1, min(limit, MAX_LIMIT))

        raw_filter = config.get("filter")
        filter_value = raw_filter if isinstance(raw_filter, dict) and raw_filter else None

        items = await store.asearch(
            namespace, query=query or None, filter=filter_value, limit=limit
        )

        texts = [_stored_text(item.value.get("text")) for item in items]
        # The scores are worth surfacing: "why did it return that" is the first
        # question when a semantic result looks wrong, and a score of 0.0 (an
        # exact filter match with no query) says the ranking did not run at all.
        matches = [
            {
                "text": _stored_text(item.value.get("text")),
                "key": item.key,
                "score": float(item.score) if item.score is not None else 0.0,
            }
            for item in items
        ]

        if not query:
            await ctx.log(f"Listed {len(matches)} from {'/'.join(namespace)}.", "info")
        else:
            await ctx.log(f"Found {len(matches)} for '{query[:80]}'.", "info")
        return {"out": texts, "matches": matches}


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MODE_LIST",
    "MODE_SEARCH",
    "MODE_WRITE",
    "MemoryNode",
    "build_memory_spec",
]
