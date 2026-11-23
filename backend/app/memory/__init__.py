"""Memory: where an agent keeps what it learned, and how it looks it up again.

Two things live here and only two:

* :mod:`app.memory.store` — the seam. Which store, which embedder, and what a
  deployment without either gets.
* :mod:`app.memory.nodes` — the canvas node that writes and recalls.

The retrieval engine, namespaces, filter, TTL and the delete are all
`langgraph.store.BaseStore`'s. Measured before this package existed: the stock
`InMemoryStore` + `OpenAIEmbeddings` against GLM answered a Chinese semantic query
correctly with no code of ours in the path. Task 19's "with retrieval" is
therefore an integration, not an implementation.
"""

from __future__ import annotations

from app.memory.goals import (
    GOALS_SEGMENT,
    GOAL_ACTIVE,
    GOAL_ABANDONED,
    GOAL_DONE,
    GOAL_STATUSES,
    GoalError,
    Todo,
    build_goal,
    is_finished,
    next_item,
    normalize_items,
    progress,
    reconcile_status,
    summarize,
    validate_objective,
)
from app.memory.store import (
    MEMORY_STORE_MEMORY,
    MEMORY_STORE_OFF,
    MEMORY_STORE_SQLITE,
    InMemoryStoreProvider,
    MemoryConfigError,
    MemoryStoreProvider,
    NoMemoryStoreProvider,
    SqliteStoreProvider,
    build_memory_store_provider,
    get_embedder,
    get_memory_store,
    get_memory_store_provider,
    read_scope,
    set_memory_store_provider,
)

__all__ = [
    "GOALS_SEGMENT",
    "GOAL_ACTIVE",
    "GOAL_ABANDONED",
    "GOAL_DONE",
    "GOAL_STATUSES",
    "MEMORY_STORE_MEMORY",
    "MEMORY_STORE_OFF",
    "MEMORY_STORE_SQLITE",
    "GoalError",
    "InMemoryStoreProvider",
    "MemoryConfigError",
    "MemoryStoreProvider",
    "NoMemoryStoreProvider",
    "SqliteStoreProvider",
    "Todo",
    "build_goal",
    "build_memory_store_provider",
    "get_embedder",
    "get_memory_store",
    "get_memory_store_provider",
    "is_finished",
    "next_item",
    "normalize_items",
    "progress",
    "read_scope",
    "reconcile_status",
    "set_memory_store_provider",
    "summarize",
    "validate_objective",
]
