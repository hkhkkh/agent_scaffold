"""Retriever node — lexical retrieval over documents you supply.

Scope, stated plainly
---------------------
This performs **keyword retrieval (BM25-style scoring)**, not vector search. It
has no embedding model and no vector store, so it works offline and with zero
setup.

That is a real limitation: it cannot match paraphrases ("car" vs "automobile").
It is the right tool for small, well-worded corpora and for prototyping the rest
of a RAG pipeline. Swapping in an embedding-backed retriever means implementing
one more ``BaseNode`` subclass and registering it — the port types
(``RETRIEVER``, ``EMBEDDINGS``) are already in place for exactly that.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from app.graph.schema import (
    InputPort,
    JsonParam,
    NodeCategory,
    NodeTypeSpec,
    NumberParam,
    OutputPort,
    PortType,
    SelectOption,
    SelectParam,
    StringParam,
)
from app.nodes.base import BaseNode, NodeContext, template_scope
from app.nodes.registry import register_node
from app.runtime.template import render

_TOKEN = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    """a an and are as at be by for from has have he in is it its of on that the
    to was were will with what which who when where how this these those i you we
    they them their or not no do does did""".split()
)


RETRIEVER_SPEC = NodeTypeSpec(
    type="retriever",
    label="Retriever",
    label_zh="检索器",
    category=NodeCategory.DATA,
    description="Rank documents by keyword relevance and return the best matches.",
    description_zh="按关键词相关度给文档排序，返回最匹配的几条。",
    icon="search",
    color="#10b981",
    inputs=[
        InputPort(
            id="in",
            label="Documents",
            label_zh="文档",
            type=PortType.ANY,
            description="Optional. Overrides the configured documents.",
            description_zh="可选。会覆盖下面配置的文档。",
        )
    ],
    outputs=[
        OutputPort(id="out", label="Matches", label_zh="匹配结果", type=PortType.TABLE),
        OutputPort(id="text", label="Joined text", label_zh="合并文本", type=PortType.TEXT),
    ],
    params=[
        JsonParam(
            name="documents",
            label="Documents",
            label_zh="文档",
            description=(
                "A list of strings, or a list of objects with 'text' and optional "
                "'metadata' fields."
            ),
            description_zh=(
                "可以是字符串列表，也可以是对象列表（对象含 'text' 字段，"
                "可选 'metadata' 字段）。"
            ),
            rows=8,
            default=[],
        ),
        StringParam(
            name="query",
            label="Query",
            label_zh="查询",
            description="What to search for. Supports {{ }} placeholders.",
            description_zh="要搜索的内容。支持 {{ }} 占位符。",
            required=True,
            default="{{ query }}",
        ),
        NumberParam(
            name="top_k",
            label="Results",
            label_zh="返回条数",
            default=4,
            min=1,
            max=50,
            step=1,
            integer=True,
        ),
        NumberParam(
            name="min_score",
            label="Minimum score",
            label_zh="最低得分",
            description="Drop matches scoring below this. 0 disables the filter.",
            description_zh="低于该分数的匹配会被丢弃。填 0 表示不过滤。",
            default=0.0,
            min=0,
            max=1,
            step=0.05,
            advanced=True,
        ),
        SelectParam(
            name="strategy",
            label="Strategy",
            label_zh="策略",
            default="bm25",
            options=[
                SelectOption(
                    label="Keyword relevance (BM25)",
                    label_zh="关键词相关度（BM25）",
                    value="bm25",
                ),
                SelectOption(label="Substring match", label_zh="子串匹配", value="substring"),
            ],
        ),
        StringParam(
            name="separator",
            label="Join separator",
            label_zh="连接分隔符",
            description="Used when combining matches into the text output.",
            description_zh="把匹配结果拼成文本输出时使用。",
            default="\n\n---\n\n",
            multiline=True,
            rows=2,
            advanced=True,
        ),
    ],
    ai_notes="""\
**This is keyword search, not semantic search.** `bm25` scores term overlap and
`substring` looks for literal text; neither has embeddings. A query for
"automobile" will not retrieve a document that only ever says "car". Do not
describe this node to a user as a vector search or a knowledge base, and do not
reach for it when the task is "find documents that mean the same thing".

* **Documents arrive on the `in` handle**, or from the `documents` parameter.
  Wiring is preferred: the parameter is a template, so a large list has to be
  serialised into JSON text to get there.
* Accepted shapes: a list of strings, or a list of objects carrying a `text`,
  `content` or `page_content` key. A list of objects with none of those keys is
  JSON-encoded whole, which scores badly and looks like a relevance bug.
* **A document list nested inside a dict retrieves nothing.** Check what the
  upstream Aggregator actually emitted — a `label` on it wraps the list in
  `{label: [...]}` and this node then reports "no documents to search".
* `out` is the ranked list with scores; `text` is the matches joined by
  `separator`. Downstream prompt nodes want `text`.\
""",
)


@register_node
class RetrieverNode(BaseNode):
    spec = RETRIEVER_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        config = self.resolve_config(ctx)

        scope = template_scope(ctx, config)

        query = render(config.get("query") or "", scope).strip()
        if not query:
            raise ValueError("Retriever node has an empty query.")

        documents = _normalise_documents(
            ctx.inputs.get("in") if ctx.inputs.get("in") is not None else config.get("documents")
        )
        if not documents:
            await ctx.log("No documents to search; returning no matches.", "warning")
            return {"out": [], "text": ""}

        top_k = int(config.get("top_k") or 4)
        min_score = float(config.get("min_score") or 0.0)

        if (config.get("strategy") or "bm25") == "substring":
            scored = _substring_scores(query, documents)
        else:
            scored = _bm25_scores(query, documents)

        hits = [entry for entry in scored if entry["score"] >= min_score][:top_k]
        await ctx.log(
            f"Searched {len(documents)} document(s); kept {len(hits)} "
            f"above score {min_score:g}.",
            "info",
        )

        separator = config.get("separator") or "\n\n---\n\n"
        joined = separator.join(str(hit["text"]) for hit in hits)
        return {"out": hits, "text": joined}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    return [
        token
        for token in _TOKEN.findall(str(text).lower())
        if token not in _STOPWORDS and len(token) > 1
    ]


def _bm25_scores(query: str, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Okapi BM25 over the supplied documents.

    Implemented inline rather than pulling in a dependency: it is ~30 lines and
    keeps the retriever inspectable, which matters for a tool whose ranking you
    will eventually need to debug.
    """
    tokenized = [_tokenize(doc["text"]) for doc in documents]
    lengths = [len(tokens) for tokens in tokenized]
    average_length = sum(lengths) / len(lengths) if lengths else 0.0

    query_terms = _tokenize(query)
    if not query_terms:
        return [{"text": d["text"], "score": 0.0, "metadata": d["metadata"]} for d in documents]

    document_frequency = Counter()
    for tokens in tokenized:
        for term in set(tokens):
            document_frequency[term] += 1

    k1, b = 1.5, 0.75
    total_docs = len(documents)
    results: list[dict[str, Any]] = []

    for index, tokens in enumerate(tokenized):
        counts = Counter(tokens)
        length = lengths[index] or 1
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if frequency == 0:
                continue
            df = document_frequency[term]
            idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (1 - b + b * length / (average_length or 1))
            score += idf * (frequency * (k1 + 1)) / denominator
        results.append(
            {
                "text": documents[index]["text"],
                "metadata": documents[index]["metadata"],
                "score": round(score, 4),
            }
        )

    # Normalise to 0..1 so `min_score` means the same thing across corpora.
    highest = max((r["score"] for r in results), default=0.0)
    if highest > 0:
        for result in results:
            result["score"] = round(result["score"] / highest, 4)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def _substring_scores(query: str, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    needle = query.lower()
    results: list[dict[str, Any]] = []
    for doc in documents:
        haystack = str(doc["text"]).lower()
        if needle and needle in haystack:
            # Longer overlap relative to document length ranks higher.
            score = min(1.0, len(needle) / max(len(haystack), 1) * 10)
        else:
            score = 0.0
        results.append(
            {"text": doc["text"], "metadata": doc["metadata"], "score": round(score, 4)}
        )
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def _normalise_documents(raw: Any) -> list[dict[str, Any]]:
    """Accept a list of strings, objects, a JSON string, or a single string."""
    if raw in (None, "", []):
        return []

    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("[") or text.startswith("{"):
            import json

            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                raw = [text]
        else:
            raw = [text]

    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raw = [raw]

    documents: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            documents.append({"text": item, "metadata": {}})
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content") or item.get("page_content")
            if text is None:
                import json

                text = json.dumps(item, ensure_ascii=False, default=str)
            documents.append(
                {
                    "text": str(text),
                    "metadata": item.get("metadata") or {
                        k: v for k, v in item.items() if k not in ("text", "content")
                    },
                }
            )
    return documents


__all__ = ["RETRIEVER_SPEC", "RetrieverNode"]
