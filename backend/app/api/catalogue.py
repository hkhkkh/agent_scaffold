"""Node type, tool, and state-field catalogue.

These endpoints are what make the frontend *generic*: the palette, the Inspector
form, and the connection validator all read from here rather than hard-coding a
node list. Adding a node type on the backend makes it appear in the UI with no
frontend change.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.graph.schema import (
    PORT_COLORS,
    NodeTypeSpec,
    PortType,
    ReducerType,
    RunStatus,
    StateFieldType,
    localize,
    normalize_language,
)
from app.nodes import list_node_type_specs
from app.nodes.registry import all_params_for, refresh_dynamic_specs
from app.security.policy import require_admin
from app.settings import get_settings
from app.tools import list_tools

router = APIRouter(tags=["catalogue"])

#: Shared declaration so every catalogue endpoint accepts the language the same
#: way, and so the OpenAPI docs describe it once.
LangQuery = Query(
    default="en",
    description=(
        "Language for human-readable text. Node ids, port ids, and config keys "
        "stay language-independent; only labels and descriptions change."
    ),
)


@router.get("/node-types", summary="All registered node types")
def get_node_types(lang: str = LangQuery) -> dict[str, Any]:
    """Every node type, with its ports, params, and metadata.

    The frontend renders the palette from ``category``, the node card from
    ``color``/``icon``, and the Inspector from ``params``.

    Localisation happens here rather than in the client so the generic renderer
    keeps reading plain ``label``/``description``: switching language re-fetches
    the catalogue, and no component needs to know a locale exists.
    """
    language = normalize_language(lang)
    # Rebuild the specs whose contents depend on runtime state (the Tool node's
    # dropdown is the tool registry) before serialising. Without this the palette
    # would show whatever the registry held at import time.
    refresh_dynamic_specs()
    specs = list_node_type_specs()
    return {
        "count": len(specs),
        "lang": language,
        "types": [_serialize(spec, language) for spec in specs],
        "port_colors": {port.value: color for port, color in PORT_COLORS.items()},
    }


def _serialize(spec: NodeTypeSpec, language: str) -> dict[str, Any]:
    """One node type for the palette and the Inspector.

    The parameters are taken from :func:`all_params_for` rather than from
    ``spec.params`` so the resilience knobs — timeout, retries, `on_error` —
    appear on every node without being declared by each one. Serialising the spec
    directly was where those four would have been silently absent.
    """
    payload = spec.model_dump(mode="json")
    payload["params"] = [
        param.model_dump(mode="json") for param in all_params_for(spec)
    ]
    settings = get_settings()
    if spec.type == "code" and (settings.public_demo or not settings.code_node_enabled):
        # Keep the spec in the payload. The canvas uses it to render nodes that
        # are already in a saved graph; omitting it would make `graphFields()`
        # drop every Code node the moment the workflow is opened.
        payload["disabled"] = True
        payload["disabled_reason"] = (
            "Code execution is disabled in this deployment. Existing Code nodes "
            "are display-only."
        )
        payload["disabled_reason_zh"] = (
            "此部署已禁用代码执行。已有的 Code 节点仅可查看。"
        )
    if settings.public_demo:
        from app.nodes.integrations import PUBLIC_DEMO_DISABLED_TYPES

        if spec.type in PUBLIC_DEMO_DISABLED_TYPES:
            payload["disabled"] = True
            payload["disabled_reason"] = (
                "This connector is disabled in the public demo. It needs a "
                "private deployment with credentials and network access."
            )
            payload["disabled_reason_zh"] = (
                "此连接器在公开演示中禁用，需要带凭据和网络权限的私有部署。"
            )
    return localize(payload, language)


@router.get("/tools", summary="All registered tools")
def get_tools(lang: str = LangQuery) -> dict[str, Any]:
    """Tools an Agent node can call, with their JSON schemas.

    Only the display strings are localised. The JSON Schema's own parameter
    descriptions are deliberately left alone: they are what the model reads.
    """
    language = normalize_language(lang)
    tools = list_tools()
    return {
        "count": len(tools),
        "lang": language,
        "tools": [localize(tool.summary(), language) for tool in tools],
    }


@router.get("/state-fields", summary="Available shared-state field options")
def get_state_fields(lang: str = LangQuery) -> dict[str, Any]:
    """Enumerations the UI needs when editing a workflow's state schema."""
    language = normalize_language(lang)

    # The field-type names are identifiers rendered by title-casing, so their
    # Chinese form is a lookup rather than a transformation.
    type_labels = {
        "text": "文本",
        "number": "数字",
        "boolean": "布尔",
        "json": "JSON",
        "list": "列表",
        "messages": "消息",
    }

    reducers = [
        {
            "value": ReducerType.OVERWRITE.value,
            "label": "Overwrite",
            "label_zh": "覆盖",
            "description": "The newest value wins. Correct for most fields.",
            "description_zh": "新值覆盖旧值。大多数字段用这个。",
        },
        {
            "value": ReducerType.APPEND.value,
            "label": "Append to list",
            "label_zh": "追加到列表",
            "description": "Accumulate values across steps, e.g. a log.",
            "description_zh": "把各步骤的值累积起来，例如日志。",
        },
        {
            "value": ReducerType.ADD_MESSAGES.value,
            "label": "Message history",
            "label_zh": "消息历史",
            "description": "Append chat messages, replacing any with a matching id.",
            "description_zh": "追加对话消息；同 id 的会被替换。",
        },
        {
            "value": ReducerType.MERGE_DICT.value,
            "label": "Merge objects",
            "label_zh": "合并对象",
            "description": "Shallow-merge dicts so parallel branches each add a key.",
            "description_zh": "浅合并字典，便于并行分支各写一个键。",
        },
    ]

    payload = {
        "types": [
            {
                "value": member.value,
                "label": member.value.replace("_", " ").title(),
                "label_zh": type_labels.get(member.value, ""),
            }
            for member in StateFieldType
        ],
        "reducers": reducers,
        "ports": [
            {"value": member.value, "color": PORT_COLORS[member]}
            for member in PortType
        ],
    }

    return localize(payload, language)


@router.get("/health", summary="Liveness probe")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics", summary="Runtime counters")
def metrics(_admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Counters for a deployment to scrape.

    `delivery.md` §3 lists `GET /metrics` as part of an exported agent's surface,
    so this lives beside `/health` rather than in a monitoring module of its own.

    **JSON, not the Prometheus text format.** The choice is about dependencies:
    the text format needs `prometheus_client`, which is a new runtime dependency
    in the agent package and its vendored wheel set (`scripts/build_vendor.py`
    would have to grow it and re-seal). A plain dict is scrapable by anything
    that can read JSON, and the numbers here are a handful of gauges rather than
    a metric namespace worth a client library. Recorded in the log as the
    deliberate trade-off it is — a Prometheus-exposition endpoint is the natural
    upgrade if a real collector needs it.

    Deliberately **not** live per-run detail: `/runs` already has that, and
    duplicating it here would give an operator two answers to the same question.
    """
    from app.runtime.manager import get_run_manager
    from app.runtime.queue import get_task_queue
    from app.runtime.runs import get_run_store

    queue = get_task_queue().snapshot()
    live = get_run_manager().list(limit=200)

    return {
        # Not a health check — `/health` is. This is the number an operator
        # graphs, and it is allowed to be zero.
        "runs_in_memory": len(live),
        "runs_by_status": {
            # `list()` returns `RunContext` objects, not the wire dicts `/runs`
            # serialises — so the attribute is read directly rather than through
            # `.get()`, which would raise on a dataclass.
            # `.value`, not `str(...)`: `RunStatus` subclasses `str`, so `str()`
            # on a member gives `"RunStatus.PENDING"`.
            status.value: sum(1 for run in live if run.status is status)
            for status in (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.INTERRUPTED)
        },
        "queue": {
            "limit": queue.get("limit"),
            # `active`, not `running` — see `InMemoryTaskQueue.snapshot`.
            "running": len(queue.get("active") or []),
            "waiting": len(queue.get("waiting") or []),
        },
        # Which store is answering, so two deployments that disagree about a run
        # count can be told apart without reading their configuration.
        "run_store": get_run_store().name,
    }
