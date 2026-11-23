"""Where a run rebuild gets the definition it is resuming.

Why this module exists
----------------------
``runtime/manager.py`` needs the workflow definition to rebuild a run after a
restart (:meth:`RunManager.hydrate`). That definition lives in the platform's
database, and the manager used to reach for it directly::

    from app.db.repository import WorkflowNotFound, get_workflow

The import sat **inside** the method, so it never ran at import time and the
task-2.6 gate (``tests/test_run_store.py``) — which only walks module-level
imports — stayed green. That made it the last thing between "the runtime is
standalone" and a fact: extracting the runtime into a wheel (task 27a) copies
module *sources*, and a copied manager would carry a line naming a package that
does not exist inside an exported agent. A lazy import is a smaller problem than
an eager one, but it is the same problem.

This is the seam that removes it, and it is deliberately **the same shape as
``app/graph/subgraphs.py``** — a module-level callable, a setter, and a helper
that answers "is anything installed?". That module already solves this exact
problem (reading a definition from the runtime without importing the platform),
so this one copies it rather than inventing a second convention: no ABC, no
provider class, no null object. A missing loader is simply ``None``.

Why the loader raises instead of returning ``None``
---------------------------------------------------
``load_subgraph`` returns ``None`` for a miss because the *validator* reports it,
and a readable validation message is that layer's job. Here the only caller is
``RunManager.hydrate``, whose response to "no definition" is to refuse the resume
with an explanation — so ``None`` would make the manager invent a second branch
for a case the interface could fold in.

Why :class:`WorkflowUnavailable` and not ``WorkflowNotFound``
------------------------------------------------------------
``WorkflowNotFound`` lives in ``app.db.repository``, and importing it is the
dependency being removed. The platform translates its own exception into this one
at the injection point, so the runtime never names that class.
"""

from __future__ import annotations

from typing import Callable

from app.graph.schema import WorkflowGraph


class WorkflowUnavailable(LookupError):
    """No definition is available for the requested workflow id.

    A *lookup* failure rather than a generic error: the manager's only sensible
    response is "this run cannot be rebuilt", and the message it writes explains
    why. Raising anything else would be reported as an unexpected fault rather
    than the situation it actually is.
    """


#: How the platform reads a definition. ``None`` when nothing is installed, which
#: is the normal state of an exported agent — it runs a graph it was handed and
#: never rebuilds one.
_LOAD: Callable[[str], WorkflowGraph] | None = None


def set_workflow_loader(load: Callable[[str], WorkflowGraph] | None) -> None:
    """Install (or clear) the platform's way of reading a definition.

    The callable raises :class:`WorkflowUnavailable` for an unknown id.
    """
    global _LOAD
    _LOAD = load


def has_workflow_loader() -> bool:
    return _LOAD is not None


def load_workflow(workflow_id: str) -> WorkflowGraph:
    """The definition for ``workflow_id``.

    Raises :class:`WorkflowUnavailable` when no loader is installed — a state that
    is correct for a standalone deployment and a misconfiguration for the canvas,
    and the message says which.
    """
    if _LOAD is None:
        raise WorkflowUnavailable(
            f"No workflow loader is installed, so '{workflow_id}' cannot be "
            "loaded. A standalone deployment runs a graph it was given and never "
            "rebuilds one; the canvas installs a loader at startup."
        )
    return _LOAD(workflow_id)


__all__ = [
    "WorkflowUnavailable",
    "has_workflow_loader",
    "load_workflow",
    "set_workflow_loader",
]



