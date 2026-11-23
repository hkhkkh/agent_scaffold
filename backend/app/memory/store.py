"""Where an agent's memory lives, and what turns text into something searchable.

Scope, and why it is this small
-------------------------------
The retrieval half of memory is **entirely upstream**. `BaseStore` already offers
namespaces, filter, semantic `query`, TTL and a delete, and `InMemoryStore`
implements all of it. Measured before writing this: a store built with
`IndexConfig(embed=<GLM embeddings>, dims=2048)` answered "订单号的格式是什么？"
with the order-prefix fact at score 0.65, and namespace isolation held. So what is
left to write is the seam — one place that says *which* store and *which* embedder
a deployment uses — and nothing else.

That is the whole module. It is deliberately the same shape as
`app/runtime/checkpoints.py`: an ABC, concrete providers, a process-wide
`get_*`/`set_*`, and a `build_*` that reads Settings. When Postgres arrives, a
`PostgresStoreProvider` slots in without any caller changing.

Two independent choices
-----------------------
The **store** and the **embedder** are configured separately because they fail
differently. A deployment can have a working store and no embedding key: memory
still works, and search degrades to exact `filter` matching rather than raising.
`get_embedder()` therefore returns `None` instead of raising, and the caller
passes that `None` straight through to `IndexConfig(embed=None)` — which is the
upstream way of saying "no semantic index". There is no need for a flag of our
own to express that.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from app.settings import Settings, get_settings

logger = logging.getLogger("app.memory")

#: `MEMORY_STORE` values.
MEMORY_STORE_MEMORY = "memory"
#: On disk, so memory outlives the process. Measured before adding it: the stock
#: `AsyncSqliteStore` persists across a reopen, indexes vectors into its own
#: `store_vectors` table (so semantic search works), and `sweep_ttl()` really
#: deletes expired items. That is the whole of "long-term memory" with no server
#: to run — which is why task 13's Postgres is no longer a prerequisite for this.
MEMORY_STORE_SQLITE = "sqlite"
#: `MEMORY_STORE=off` — the store is `None`, and every memory node is a no-op.
#: Not the default: a workflow that uses a memory node should not silently do
#: nothing because a deployment left a setting alone.
MEMORY_STORE_OFF = "off"

DEFAULT_MEMORY_STORE = MEMORY_STORE_SQLITE
#: `EMBEDDING_MODEL` when the deployment wants semantic search.
DEFAULT_EMBEDDING_MODEL = ""


class MemoryConfigError(ValueError):
    """Raised when `MEMORY_STORE` names something that does not exist.

    An error rather than a fallback, matching the other capability layers: a
    typo for `off` that quietly became `memory` would be harmless, but the
    reverse would lose an operator's data expectations without saying so.
    """


class MemoryStoreProvider(ABC):
    """Supplies the store a run reads and writes, and owns its lifetime."""

    #: Value of `MEMORY_STORE` that selects this provider.
    name: ClassVar[str] = ""

    @abstractmethod
    def make(self) -> BaseStore:
        """The store a run should use.

        Called per run, like the checkpointer's `make`. An in-process
        implementation returns the same object every time, which is the point:
        memory that is per-run is not memory.
        """

    async def open(self) -> None:
        """Acquire process-wide resources. Called once at startup."""

    async def close(self) -> None:
        """Release them. Called once at shutdown."""


class NoMemoryStoreProvider(MemoryStoreProvider):
    """`MEMORY_STORE=off`. No store, so memory nodes do nothing.

    Present as a provider rather than as a `None` check at each call site so the
    configuration path stays uniform: every value of the setting resolves to a
    provider, and the provider is what decides there is nothing to hand out.
    """

    name = MEMORY_STORE_OFF

    def make(self) -> BaseStore:
        raise MemoryConfigError(
            "MEMORY_STORE=off, so there is no store to hand out. A workflow that "
            "uses a memory node needs MEMORY_STORE=memory."
        )


class InMemoryStoreProvider(MemoryStoreProvider):
    """One process-wide in-memory store, shared by every run.

    Shared deliberately. A per-run store would make `search` return only what the
    current run wrote, which is indistinguishable from a working memory during a
    single-run demo and useless in the case memory exists for: the *next* run
    remembering the last one.

    The index is built once, at construction, because `IndexConfig` carries the
    dimensions and the embedder — changing either later would invalidate vectors
    already written.
    """

    name = MEMORY_STORE_MEMORY

    def __init__(self, *, embed: Any | None = None, dims: int | None = None) -> None:
        from langgraph.store.base import IndexConfig

        index = IndexConfig(embed=embed, dims=dims) if embed is not None else None
        self._store: BaseStore = InMemoryStore(index=index)

    def make(self) -> BaseStore:
        return self._store


class SqliteStoreProvider(MemoryStoreProvider):
    """One shared on-disk store. Memory outlives the process.

    Not :class:`InMemoryStoreProvider` with a flag, because the lifetime is
    genuinely different: this one holds a connection that has to be opened before
    it can hand out a store and closed at shutdown, exactly like
    `SqliteCheckpointerProvider`.

    No TTL is configured, so entries live until deleted. That is the honest
    default for memory — an agent's fact about a customer should not evaporate on
    a timer nobody chose — and a deployment that wants expiry can be given one
    without changing any caller.
    """

    name = MEMORY_STORE_SQLITE

    def __init__(self, path: Path, *, embed: Any | None = None, dims: int | None = None) -> None:
        self.path = Path(path)
        self._embed = embed
        self._dims = dims
        self._store: BaseStore | None = None
        self._context: Any | None = None

    def make(self) -> BaseStore:
        if self._store is None:
            # Reachable if a node runs before the lifespan opened us, e.g. from a
            # test that forgot to. Failing loudly beats handing back a store that
            # writes nowhere.
            raise MemoryConfigError(
                "The sqlite memory store was used before it was opened. It is "
                "opened in the application lifespan; a caller outside that "
                "lifespan must `await get_memory_store_provider().open()` first."
            )
        return self._store

    async def open(self) -> None:
        from langgraph.store.base import IndexConfig
        from langgraph.store.sqlite.base import SqliteIndexConfig
        from langgraph.store.sqlite.aio import AsyncSqliteStore

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The vector index needs its own config class: the sqlite store keeps
        # vectors in a `store_vectors` side table and takes dimensions up front.
        # Passing the base `IndexConfig` here is accepted and then silently does
        # literal matching, which is why this branch is explicit.
        index = (
            SqliteIndexConfig(embed=self._embed, dims=self._dims)
            if self._embed is not None
            else None
        )
        self._context = AsyncSqliteStore.from_conn_string(str(self.path), index=index)
        self._store = await self._context.__aenter__()
        # Creates the tables on first use and is a no-op afterwards.
        await self._store.setup()  # type: ignore[attr-defined]
        logger.info("Memory store : %s", self.path)

    async def close(self) -> None:
        context = self._context
        self._store = None
        self._context = None
        if context is not None:
            await context.__aexit__(None, None, None)


def build_memory_store_provider(settings: Settings | None = None) -> MemoryStoreProvider:
    """Construct the configured provider, with the configured embedder.

    Raises :class:`MemoryConfigError` for an unrecognised name.
    """
    settings = settings or get_settings()
    kind = (settings.memory_store or DEFAULT_MEMORY_STORE).strip().lower()

    if kind == MEMORY_STORE_OFF:
        return NoMemoryStoreProvider()
    if kind == MEMORY_STORE_SQLITE:
        return SqliteStoreProvider(
            settings.data_dir / "memory.db",
            embed=get_embedder(settings),
            dims=settings.embedding_dims or None,
        )
    if kind == MEMORY_STORE_MEMORY:
        return InMemoryStoreProvider(
            embed=get_embedder(settings), dims=settings.embedding_dims or None
        )

    raise MemoryConfigError(
        f"MEMORY_STORE={kind!r} is not a known store. Use "
        f"'{MEMORY_STORE_SQLITE}' (on disk, so memory survives a restart), "
        f"'{MEMORY_STORE_MEMORY}' (this process) or '{MEMORY_STORE_OFF}' "
        "(memory nodes do nothing)."
    )


def get_embedder(settings: Settings | None = None) -> Any | None:
    """The embeddings object a search index should use, or `None`.

    `None` means "no semantic index", and it is a supported answer rather than a
    failure: `IndexConfig(embed=None)` is how upstream spells it, so a deployment
    without an embedding key gets substring/exact matching and no exception.

    Reused rather than rebuilt because `OpenAIEmbeddings` holds a client, and
    building one per call would open a connection per search.

    Why `OpenAIEmbeddings` and nothing of our own: GLM's `/embeddings` is
    OpenAI-shaped, so the stock class drives it with only a `base_url` and a
    `model` changed. Measured, not assumed — `embed_query`, `embed_documents` and
    `aembed_query` all returned 2048 floats against it.
    """
    settings = settings or get_settings()

    model = (settings.embedding_model or DEFAULT_EMBEDDING_MODEL).strip()
    if not model:
        return None

    key = _embedding_key(settings)
    if not key:
        # Worth a warning: the operator asked for a model and did not get one, and
        # the symptom (search matching literally) looks like a bug in the search.
        logger.warning(
            "EMBEDDING_MODEL=%s is set but no embedding key is configured; "
            "memory search will match literally instead of semantically.",
            model,
        )
        return None

    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        model=model,
        base_url=settings.embedding_base_url or settings.llm_base_url,
        api_key=key,
        **({"dimensions": settings.embedding_dims} if settings.embedding_dims else {}),
    )


def _embedding_key(settings: Settings) -> str:
    """The embedding credential, from wherever this deployment keeps credentials.

    Separate from the chat key so an operator can point embeddings at a different
    vendor — or at none — without disturbing the chat model. It **falls back** to
    the chat key because in the common case both come from the same provider, and
    asking for the same secret twice is how a deployment ends up configured
    halfway.
    """
    from app.security.secrets import (
        SECRET_EMBEDDING_API_KEY,
        SECRET_LLM_API_KEY,
        get_secret_provider,
    )

    provider = get_secret_provider()
    if provider.source == "settings":
        return settings.embedding_api_key or settings.llm_api_key or ""
    return (
        provider.get(SECRET_EMBEDDING_API_KEY)
        or provider.get(SECRET_LLM_API_KEY)
        or ""
    )


#: Process-wide provider. Swapped in tests, and the seam where Postgres plugs in.
_provider: MemoryStoreProvider | None = None


def get_memory_store_provider() -> MemoryStoreProvider:
    global _provider
    if _provider is None:
        _provider = build_memory_store_provider()
    return _provider


def set_memory_store_provider(provider: MemoryStoreProvider | None) -> None:
    """Override the process provider. ``None`` restores the configured one."""
    global _provider
    _provider = provider


def get_memory_store() -> BaseStore | None:
    """The store, or `None` when the deployment has memory switched off.

    The `None` is the point: a node asks this and skips itself, so a graph
    containing a memory node still runs on a deployment that has no memory. The
    alternative — raising — would make one node's configuration a graph-wide
    outage.
    """
    provider = get_memory_store_provider()
    if isinstance(provider, NoMemoryStoreProvider):
        return None
    return provider.make()


async def read_scope(
    store: BaseStore,
    namespace: tuple[str, ...],
    *,
    key: str | None = None,
    limit: int = 100,
) -> list[Any]:
    """Items in a namespace, optionally just one by key.

    A thin wrapper rather than calling `aget`/`asearch` at each call site, because
    the two need different handling for the same "give me this" intent: `aget`
    returns `None` for a missing key while `asearch` returns a list, and a caller
    that has to remember which is which will eventually forget. Returning a list
    either way makes `if not found:` correct in both cases.
    """
    if key is not None:
        item = await store.aget(namespace, key)
        return [item] if item is not None else []
    return list(await store.asearch(namespace, limit=limit))


__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_MEMORY_STORE",
    "MEMORY_STORE_MEMORY",
    "MEMORY_STORE_OFF",
    "MEMORY_STORE_SQLITE",
    "InMemoryStoreProvider",
    "MemoryConfigError",
    "MemoryStoreProvider",
    "NoMemoryStoreProvider",
    "SqliteStoreProvider",
    "build_memory_store_provider",
    "get_embedder",
    "get_memory_store",
    "get_memory_store_provider",
    "read_scope",
    "set_memory_store_provider",
]
