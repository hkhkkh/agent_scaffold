"""FastAPI application entry point.

Run with::

    d:\\AI_LLM_Project\\venv_d\\Scripts\\python.exe -m uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import access
from app.api import ai as ai_api
from app.api import (
    catalogue,
    files as files_api,
    hooks,
    lab,
    runs,
    schedules,
    settings as settings_api,
    workflows,
)
from app.db.migrate import ensure_schema
from app.db.session import dispose_engines
from app.memory import (
    build_memory_store_provider,
    set_memory_store_provider,
)
from app.runtime.checkpoints import (
    build_checkpointer_provider,
    set_checkpointer_provider,
)
from app.runtime.ai.gateway import set_usage_sink
from app.runtime.manager import get_run_manager
from app.runtime.queue import build_task_queue, set_task_queue
from app.runtime.runs import build_run_store, set_database_run_store, set_run_store
from app.runtime.workflows import set_workflow_loader
from app.scheduling.service import (
    MISFIRE_GRACE_SECONDS,
    build_scheduler,
    set_scheduler,
)
from app.runtime.trace import TraceIdFilter
from app.security.deps import AuthMiddleware, TraceMiddleware, configure
from app.security.gate import GateMiddleware
from app.security.policy import (
    AccessDenied,
    PublicDemoMiddleware,
    language_for,
    message as policy_message,
)
from app.security.quotas import QuotaMiddleware, record_usage
from app.security.ratelimit import RateLimitMiddleware
from app.security.secrets import (
    SECRET_LLM_API_KEY,
    get_secret_provider,
)
from app.security.web import (
    BodySizeLimitMiddleware,
    HostCheckMiddleware,
    OriginCheckMiddleware,
    SecurityHeadersMiddleware,
)
from app.settings import get_settings
from app.tools import register_capability_pack_tools
from app.tools.registry import get_tool_registry, unknown_tool_names
from app.web import landing_response, register_spa_routes


def _describe_secrets() -> str:
    """Which secrets are present. Never the values — this goes to a log file."""
    described = get_secret_provider().describe()
    if not described:
        return "none declared"
    return ", ".join(
        f"{name}={'present' if present else 'absent'}"
        for name, present in sorted(described.items())
    )

logging.basicConfig(
    level=logging.INFO,
    # `trace_id` comes from `TraceIdFilter` below, which is installed on the
    # handler basicConfig creates. Every record gets the column whether its
    # author asked for it or not, and it is `-` outside a request so the columns
    # stay aligned.
    format="%(asctime)s  %(levelname)-7s [%(trace_id)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# On the handler, not the logger: `basicConfig` may have attached one already,
# and a filter on the `app` logger would miss `app.runtime.*` records.
for _handler in logging.getLogger().handlers:
    _handler.addFilter(TraceIdFilter())
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()
    # Fail closed before touching the database or starting the scheduler. A
    # public deployment with Code enabled, no gate secret, or a missing frontend
    # build must not reach the first request.
    settings.validate_public()
    logger.info(
        "Public       : %s",
        (
            f"enabled ({settings.public_origin}, gate=on, demo={settings.public_demo}, "
            f"turnstile={'on' if settings.turnstile_site_key else 'off'})"
            if settings.public_access
            else "disabled (local development)"
        ),
    )
    # Through the migration layer, not `create_all()`. `create_all` cannot evolve
    # an existing schema, so the first added column would have meant asking every
    # deployment to delete its database. See `app/db/migrate.py` for which branch
    # a given database takes and why both paths coexist.
    schema_state = ensure_schema().replace("\n", " ")
    logger.info("Schema       : %s", schema_state)
    logger.info("Data directory: %s", settings.data_dir)
    logger.info("LLM endpoint : %s (%s)", settings.llm_base_url, settings.llm_model)
    # Through the secret provider, not `settings.llm_api_key`: a deployment that
    # configures SECRETS_FILE has a key that this setting does not know about,
    # and warning "no API key" at it would be both wrong and alarming.
    if not get_secret_provider().has(SECRET_LLM_API_KEY):
        logger.warning(
            "No LLM API key configured - the backend will use a mock model. "
            "Runs and the UI work; model responses are simulated."
        )
    logger.info("Secrets      : %s", _describe_secrets())

    # Resolved here rather than at import time so a misconfiguration stops the
    # process with a clear message instead of surfacing as 500s later. Note this
    # may escalate `local` to `token` — see app/security/auth.py.
    state = configure(
        mode=settings.auth_mode,
        host=settings.server_host,
        token_specs=list(settings.auth_tokens),
        salt=settings.auth_salt or None,
    )
    logger.info("Security     : %s", state.posture())
    if state.mode == "local" and settings.server_host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(
            "AUTH_MODE=local but SERVER_HOST=%s is not a loopback address. "
            "Requests from other machines are refused until this matches the "
            "real bind address.",
            settings.server_host,
        )

    # Validated here, not in `build_tool_registry`, because the registry is built
    # while `app.tools.builtin` is still importing — every built-in tool would be
    # reported as unknown. By now the import is finished.
    #
    # A warning rather than a hard failure: a name may legitimately refer to a
    # tool a plugin registers later. But a typo in TOOLS_ALLOWLIST silently
    # produces an agent with fewer tools than intended, and that is exactly the
    # kind of misconfiguration an operator never thinks to look for.
    # Capability packs register into the same process registry the built-in
    # tools use, so the palette, the validator and the Tool node see one list.
    # Called here rather than at import: `app.tools` is imported while the pack
    # modules themselves are initialising (the core pack resolves its tools from
    # this registry), so an import-time call would recurse. A typo'd allowlist
    # name is still reported below, after the packs have landed.
    added_pack_tools = register_capability_pack_tools()
    if added_pack_tools:
        logger.info("Capability pack tools: %s", ", ".join(added_pack_tools))

    registry = get_tool_registry()
    for field, names in (
        ("TOOLS_ALLOWLIST", settings.tools_allowlist),
        ("TOOLS_DENYLIST", settings.tools_denylist),
    ):
        unknown = unknown_tool_names(list(names))
        if unknown:
            logger.warning(
                "%s names %d unrecognised tool(s): %s. Registered: %s",
                field,
                len(unknown),
                ", ".join(unknown),
                ", ".join(registry.names()) or "none",
            )
    logger.info("Tools        : %s", ", ".join(registry.names()) or "none")

    # Built here rather than lazily on first use, so an unrecognised RUN_STORE
    # stops the process with a clear message instead of surfacing as a run that
    # silently records nothing. Same reason SANDBOX_RUNNER is resolved eagerly.
    # Injected rather than imported by name: the runtime is not allowed to name
    # `app.db` (task 27a), so the platform supplies the store and the runtime
    # only knows that the configured name `sqlite` means "the one injected here".
    from app.db.run_store import SqliteRunStore

    set_database_run_store(SqliteRunStore)
    store = build_run_store(settings)
    set_run_store(store)
    logger.info("Run history  : %s", store.name)

    # Built here for the same reason as the store: an unrecognised TASK_QUEUE must
    # stop the process, not surface later as runs that never start.
    queue = build_task_queue(settings)
    set_task_queue(queue)
    logger.info(
        "Concurrency  : %s",
        "unlimited" if queue.limit == 0 else f"max {queue.limit} concurrent",
    )

    # Opened here because a durable checkpointer owns a connection that has to
    # live as long as the process, which a per-run factory cannot express. Same
    # eager-validation reason as above: an unrecognised CHECKPOINT_STORE should
    # stop the process rather than surface as a run that silently cannot resume.
    checkpoint_provider = build_checkpointer_provider(settings)
    set_checkpointer_provider(checkpoint_provider)
    await checkpoint_provider.open()
    logger.info("Checkpoints  : %s", checkpoint_provider.name)

    # Memory owns a connection too when it is file-backed, so it is opened and
    # closed in the same place for the same reason. Eager validation as well: an
    # unrecognised MEMORY_STORE should stop the process rather than surface later
    # as a memory node that silently did nothing.
    memory_provider = build_memory_store_provider(settings)
    set_memory_store_provider(memory_provider)
    await memory_provider.open()
    logger.info("Memory store : %s", memory_provider.name)

    # Where sub-flow nodes read the workflow they reference. Registered here,
    # from the platform side, because `app.graph` must stay importable without the
    # platform's database (see `app/graph/subgraphs.py`).
    from app.db.repository import list_workflow_options, load_workflow_or_none
    from app.graph.subgraphs import set_subgraph_provider

    set_subgraph_provider(load_workflow_or_none, list_workflow_options)

    # Where `RunManager.hydrate` reads the definition it is rebuilding a run from.
    # Registered from the platform side for the same reason as the subgraph
    # loader: `runtime/manager.py` must not name `app.db`, even in a lazy import,
    # or the runtime cannot be copied into an exported agent (task 27a).
    # Same shape as `set_subgraph_provider` just above: a plain function, not a
    # class. The platform's exception is translated here, at the boundary, so the
    # runtime never imports `app.db.repository`.
    from app.db.repository import WorkflowNotFound, get_workflow
    from app.runtime.workflows import WorkflowUnavailable

    def _load_workflow(workflow_id: str):
        try:
            return get_workflow(workflow_id)
        except WorkflowNotFound as exc:
            raise WorkflowUnavailable(str(exc)) from exc

    set_workflow_loader(_load_workflow)

    # The scheduler is the first long-lived background task in this project, so its
    # lifecycle is explicit: built and started here, stopped below. Built eagerly
    # like every other provider so a bad MISSED_RUN_POLICY is in the startup log.
    #
    # Reconciliation runs *before* the jobs are registered, so a schedule whose
    # workflow was deleted while the platform was down is marked orphaned rather
    # than registered and then attempted once. Without that ordering the very first
    # tick fires against a missing workflow and records a failure that is not really
    # the workflow's fault.
    scheduler = build_scheduler(settings)
    set_scheduler(scheduler)
    scheduler.reconcile()
    scheduler.start()
    logger.info(
        "Schedules    : %s (%s, misfire grace %ds)",
        "enabled" if scheduler.enabled else "disabled",
        scheduler.missed_policy,
        MISFIRE_GRACE_SECONDS,
    )

    yield
    # Stop the scheduler before the run manager and before the database engines are
    # disposed. Order matters: the job function calls into both, so stopping it last
    # would mean it can still be mid-call when the engine pool goes away. `stop()` is
    # awaited for the same reason — a bare shutdown leaves jobs running on a loop we
    # are about to tear down.
    await scheduler.stop()
    # Cancel in-flight runs so a reload does not leave orphaned tasks behind.
    await get_run_manager().shutdown()
    await memory_provider.close()
    await checkpoint_provider.close()
    dispose_engines()


# The runtime gateway stays platform-free; the web app opts into quota
# accounting by installing this sink. Exported agents never see it.
set_usage_sink(record_usage)

_public = get_settings().public_access

app = FastAPI(
    title="Agent Scaffold",
    description=(
        "Backend for a visual AI-agent workflow builder: define a graph on a "
        "canvas, let the AI fill in each node, then run it and inspect every step."
    ),
    version="0.1.0",
    lifespan=lifespan,
    # The schema is a map of the control surface. In public mode the surface is
    # gate-only and the map is not a feature; the middleware also 404s these
    # paths, but not registering them is the cheaper half.
    docs_url=None if _public else "/docs",
    redoc_url=None if _public else "/redoc",
    openapi_url=None if _public else "/openapi.json",
)

# Order matters, and this order is the safe one. `add_middleware` wraps, so the
# last one added is outermost. The public layers are added outside Auth because
# a visitor must pass the human gate before the local principal is granted; CORS
# stays outside the gate so a preflight never needs a cookie.
#
# Trace remains outermost so that *every* response carries an id, including a
# preflight and a rejected gate request. A rejected request is exactly the one
# worth tracing.
app.add_middleware(AuthMiddleware)
# Quota is inside the gate: an unverified request must not burn a visitor's
# daily allowance before it has even passed human verification.
app.add_middleware(QuotaMiddleware)
app.add_middleware(GateMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(PublicDemoMiddleware)
app.add_middleware(HostCheckMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Without this the browser cannot read the id off a cross-origin response,
    # which is the only place a frontend would look for it.
    expose_headers=["traceparent"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(TraceMiddleware)


@app.exception_handler(AccessDenied)
async def access_denied_handler(request: Request, exc: AccessDenied) -> JSONResponse:
    """A policy refusal, with a stable code the frontend can act on."""
    detail = exc.detail or policy_message(exc.code, language_for(request))
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "detail": detail},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Do not echo request input back to a public caller.

    FastAPI's default includes the offending value under ``input`` and sometimes
    a context object. In a public deployment those can carry a workflow body,
    a prompt, or a credential-shaped string, and the field name plus the reason
    is all the client actually needs.
    """
    settings = get_settings()
    errors = [
        {
            "loc": list(error.get("loc") or []),
            "msg": error.get("msg") or "Invalid value.",
            "type": error.get("type") or "value_error",
        }
        for error in exc.errors()
    ]
    if settings.public_access:
        return JSONResponse(status_code=422, content={"detail": errors})
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a useful message instead of a bare 500.

    The exception *type* is disclosed because it is what makes a local failure
    diagnosable. The message is not echoed when authentication is active: an
    unhandled error can carry a path, a query fragment, or a credential that
    appeared in a stack local, and only the operator should be reading that.
    Public mode is stricter still: a gate visitor gets no exception detail at
    all, because `AUTH_MODE=local` would otherwise take the local branch.
    """
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)

    from app.security.deps import get_state

    settings = get_settings()
    if get_state().mode == "local" and not settings.public_access:
        detail = f"{type(exc).__name__}: {exc}"
    elif settings.public_access:
        detail = "Internal server error. The full message is in the server log."
    else:
        detail = (
            f"{type(exc).__name__}. The full message is in the server log "
            "(authentication is enabled, so error detail is not sent to clients)."
        )
    return JSONResponse(
        status_code=500,
        content={"detail": detail, "path": request.url.path},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

app.include_router(access.router, prefix="/api")
app.include_router(catalogue.router, prefix="/api")
# Read-only: the canvas's live view of the results database an agent writes to.
# A window, not a control surface — the tools in `app.tools.results_db` are what
# write, and they are reachable only from a graph.
app.include_router(lab.router, prefix="/api")
app.include_router(workflows.router, prefix="/api")
app.include_router(runs.router, prefix="/api")
app.include_router(ai_api.router, prefix="/api")
app.include_router(settings_api.router, prefix="/api")
app.include_router(schedules.router, prefix="/api")
# The same prefix as the schedules router, and deliberately so: a webhook is a
# trigger, the list page shows both kinds, and the token is looked up rather than
# the id. No auth guard here — the token *is* the guard; see `app/api/hooks.py`.
app.include_router(hooks.router, prefix="/api")
# The workspace the files capability pack reads and writes. Upload/list/download
# only: the pack's tools are what an agent calls, and they share this workspace.
# Disabled in the public demo inside the router.
app.include_router(files_api.router, prefix="/api")


@app.get("/health", include_in_schema=False)
def health_alias() -> dict[str, str]:
    """The same liveness answer as `/api/health`, for tunnel/proxy probes."""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root():
    if get_settings().public_access:
        # `/` is the landing page; `/app` is the editor shell.
        return landing_response(get_settings())
    return {
        "service": "agent-scaffold",
        "docs": "/docs",
        "health": "/api/health",
    }


# Registered last: the SPA's catch-all must not shadow an API route, and an
# unknown `/api/*` must stay JSON. No-op outside public mode.
register_spa_routes(app)
