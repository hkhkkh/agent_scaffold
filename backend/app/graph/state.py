"""Shared graph state.

Design decision worth stating up front
--------------------------------------
Edge-based value passing alone is **not sufficient** for an agent workflow. It
cannot express:

* a branch that reads a value produced before the branch point,
* a node that reads a non-adjacent ancestor's output,
* a loop that accumulates across iterations,
* a conversation history that several nodes append to.

So the graph gets one **shared state** that every node may read, and nodes write
their result into ``vars[<node_id>]``. Edges then carry *dependencies* (who runs
after whom) rather than being the only data channel. This mirrors LangGraph's
``State`` + reducers, Flowise's ``$flow.state``, and Dify's variable model.

The channels below are fixed scaffolding; user-declared fields from
``WorkflowGraph.state_schema`` are layered on top.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Callable

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from app.graph.schema import ReducerType, StateField, StateFieldType

#: Node ids are stable and unique, so a merge reducer is safe for ``vars`` even
#: when several branches write in the same super-step.
def _merge_dict(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


REDUCERS: dict[ReducerType, Callable[[Any, Any], Any]] = {
    ReducerType.OVERWRITE: lambda _left, right: right,
    ReducerType.APPEND: operator.add,
    ReducerType.ADD_MESSAGES: add_messages,
    ReducerType.MERGE_DICT: _merge_dict,
}

#: Python annotation used for each declared field type.
ANNOTATIONS: dict[StateFieldType, type] = {
    StateFieldType.TEXT: str,
    StateFieldType.NUMBER: float,
    StateFieldType.BOOLEAN: bool,
    StateFieldType.JSON: dict,
    StateFieldType.LIST: list,
    StateFieldType.MESSAGES: list,
}

#: Channel names the runtime owns. A user field may not shadow these.
RESERVED_CHANNELS = frozenset({"vars", "inputs", "messages", "output"})


class GraphState(TypedDict, total=False):
    """Base state every compiled graph has.

    ``total=False`` so a node may return a partial update, which is the whole
    point of the reducer model.
    """

    #: Run inputs, seeded once by the Start node.
    inputs: dict[str, Any]
    #: ``vars[node_id][output_handle] = value`` — the primary data channel.
    vars: Annotated[dict[str, Any], _merge_dict]
    #: Conversation history, when a workflow uses it.
    messages: Annotated[list[Any], add_messages]
    #: Set by the End node; surfaced as the run result.
    output: dict[str, Any]


def build_state_schema(fields: list[StateField]) -> type[TypedDict]:
    """Compose the runtime state class from the base channels plus user fields.

    User fields are attached through ``Annotated[type, reducer]`` so a declared
    field behaves exactly like a built-in channel.
    """
    annotations: dict[str, Any] = dict(GraphState.__annotations__)

    for field in fields:
        if field.name in RESERVED_CHANNELS:
            # `vars` and `messages` are already defined; a redefinition would
            # silently drop the built-in reducer, so skip rather than corrupt.
            continue
        base = ANNOTATIONS.get(field.type, Any)
        reducer = REDUCERS.get(field.reducer, REDUCERS[ReducerType.OVERWRITE])
        annotations[field.name] = Annotated[base, reducer]

    return TypedDict("CompiledGraphState", annotations, total=False)  # type: ignore[operator]


def default_state_schema() -> list[StateField]:
    """The channels every new workflow starts with."""
    return [
        StateField(
            name="vars",
            type=StateFieldType.JSON,
            reducer=ReducerType.MERGE_DICT,
            default={},
            description="Node outputs, keyed by node id.",
        ),
        StateField(
            name="messages",
            type=StateFieldType.MESSAGES,
            reducer=ReducerType.ADD_MESSAGES,
            default=[],
            description="Conversation history, when used.",
        ),
    ]


__all__ = [
    "ANNOTATIONS",
    "GraphState",
    "REDUCERS",
    "RESERVED_CHANNELS",
    "build_state_schema",
    "default_state_schema",
]
