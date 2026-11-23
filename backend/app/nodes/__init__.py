"""Node type package.

Importing this package registers every built-in node type. Each module calls
``@register_node`` at import time, so the list below *is* the node catalogue.

To add a node type:
    1. create ``app/nodes/my_node.py`` with a ``NodeTypeSpec`` + ``BaseNode``,
    2. decorate the class with ``@register_node``,
    3. import the module here.

Nothing on the frontend needs to change — the palette, the Inspector form, the
validator, and the AI prompts all read from the registry.
"""

from __future__ import annotations

# --- core (Phase 2) -------------------------------------------------------
from app.nodes import code as _code  # noqa: F401
from app.nodes import condition as _condition  # noqa: F401
from app.nodes import end as _end  # noqa: F401
from app.nodes import llm as _llm  # noqa: F401
from app.nodes import prompt as _prompt  # noqa: F401
from app.nodes import start as _start  # noqa: F401

# --- extended (Phase 4) ---------------------------------------------------
from app.nodes import agent as _agent  # noqa: F401
from app.nodes import control as _control  # noqa: F401
from app.nodes import human as _human  # noqa: F401
from app.nodes import retriever as _retriever  # noqa: F401
from app.nodes import subgraph as _subgraph  # noqa: F401
from app.nodes import supervisor as _supervisor  # noqa: F401
from app.nodes import tool as _tool  # noqa: F401
# Business connectors: API / FHIR / SQL / email / PDF text. Separate module so
# the guards (egress, read-only SQL, DATA_DIR-only files) live together.
from app.nodes import integrations as _integrations  # noqa: F401
# Not under `app/nodes/` like the rest. Memory is a capability with its own
# store and lifetime (see `app/memory/store.py`), and the node is one caller of
# it rather than the reason it exists — so the package owns both, and this line
# is what puts it in the catalogue.
from app.memory import goals_node as _goals  # noqa: F401
from app.memory import nodes as _memory  # noqa: F401

from app.nodes.base import BaseNode, NodeContext
from app.nodes.registry import (
    NodeTypeError,
    create_node_instance,
    effective_inputs,
    effective_outputs,
    get_node_class,
    get_node_type_spec,
    has_node_type,
    list_node_type_specs,
    register_node,
)

__all__ = [
    "BaseNode",
    "NodeContext",
    "NodeTypeError",
    "create_node_instance",
    "effective_inputs",
    "effective_outputs",
    "get_node_class",
    "get_node_type_spec",
    "has_node_type",
    "list_node_type_specs",
    "register_node",
]
