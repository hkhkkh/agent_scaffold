"""Application settings.

All configuration is environment-driven so the same code runs for OpenAI,
DeepSeek, Ollama, vLLM, or any other OpenAI-compatible endpoint.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# h:\agent_scaffold\backend\app\settings.py -> parents[2] == h:\agent_scaffold
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"


class Settings(BaseSettings):
    """Runtime configuration, read from environment or the root `.env` file."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- LLM ---
    llm_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="OpenAI-compatible base URL. Point at DeepSeek/Ollama/vLLM freely.",
    )
    llm_api_key: str = Field(default="", description="API key; use 'ollama' for local servers.")
    llm_model: str = Field(default="gpt-4o-mini")
    llm_temperature: float = 0.0
    llm_timeout: float = 120.0
    llm_max_retries: int = 2
    llm_thinking: str = Field(
        default="auto",
        description=(
            "Native reasoning: auto (enable for DeepSeek), enabled, or disabled. "
            "Auto keeps non-DeepSeek OpenAI-compatible endpoints on their defaults."
        ),
    )
    llm_reasoning_effort: str = Field(
        default="high",
        description="DeepSeek thinking effort: low, high, or max.",
    )

    # ------------------------------------------------------------ Storage ---
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    database_url: str = Field(default="")

    # ------------------------------------------------------------ Runtime ---
    recursion_limit: int = Field(
        default=50,
        description="Hard cap on LangGraph super-steps. Loops MUST have a ceiling.",
    )
    # The time budget for one node attempt, enforced by `runtime/attempts.py`.
    #
    # Until task 18 this setting was only ever read by the Code node, which
    # forwards it to the sandbox so the child process can be killed. It is now
    # the default budget for *every* node, and the Code node still forwards it —
    # so for Code nodes two deadlines exist. That is intentional and the race is
    # resolved rather than left to chance: the executor waits
    # `node_timeout_seconds + GRACE_SECONDS`, so the sandbox's specific message
    # wins and this layer only speaks up if the sandbox itself hangs.
    node_timeout_seconds: float = Field(
        default=120.0,
        description=(
            "Time budget for a single node attempt. 0 or less disables the "
            "limit. The executor waits a few seconds longer than this so a "
            "node's own deadline reports its more specific error first."
        ),
    )
    # Off by default, and that is the whole point: a timeout can only cut a node
    # short, while a retry repeats whatever the node already did. Charging a
    # card, sending a message, inserting a row — all of it happens twice. An
    # author has to ask for that. Clamped to `attempts.MAX_RETRIES_CEILING`.
    node_max_retries: int = Field(
        default=0,
        description=(
            "How many times a failed node attempt is retried. 0 disables "
            "retries. Values above 5 are clamped, because retries repeat side "
            "effects."
        ),
    )

    # How long a *finished* run stays addressable in `runtime.manager`. The
    # in-memory run map is a cache for live runs, not the record of truth (the
    # `Run`/`RunStep` rows and the transcript on disk are). A terminal run keeps
    # no state anything needs — except an SSE client that is still draining, or
    # a browser reconnecting a second after the run ended. This is that window.
    #
    # 0 means "evict as soon as the transcript is written", which frees memory
    # fastest but makes a reconnect after the terminal event a 404.
    run_retention_seconds: float = Field(
        default=300.0,
        description=(
            "Seconds a finished run stays addressable before being dropped from "
            "memory. 0 evicts immediately."
        ),
    )
    # The TTL alone does not bound anything: a burst of 5000 runs inside the
    # window would still hold 5000 RunContexts. This is the hard ceiling.
    # Interrupted runs are exempt from both limits — they are waiting on a human
    # and cannot be recreated from disk.
    max_retained_runs: int = Field(
        default=200,
        description=(
            "Hard cap on how many runs are kept in memory. Interrupted runs are "
            "exempt because only they can be resumed."
        ),
    )
    code_node_enabled: bool = Field(
        default=True,
        description="Allow Code nodes to execute Python. See SECURITY note in README.",
    )
    # How many runs may execute at the same time. See `app/runtime/queue.py`.
    #
    # The failure this prevents is not a crash: past a certain number of
    # concurrent runs everything gets slower at once, including the request that
    # would have reported the problem. `0` means unlimited, which is right for a
    # single-operator dev box and wrong for anything shared.
    #
    # Runs beyond the limit **wait** rather than being rejected — the UI shows
    # them as `queued` with their position, and `cancel` takes them out of line.
    max_concurrent_runs: int = Field(
        default=0,
        description=(
            "Maximum runs executing at once. 0 disables the limit; excess runs "
            "wait in a visible queue instead of failing."
        ),
    )
    # Which queue implementation a deployment gets. See `app/runtime/queue.py`.
    task_queue: str = Field(
        default="memory",
        description=(
            "'memory' (this process). A broker-backed queue is deliberately not "
            "the default: an exported agent has no Redis."
        ),
    )
    # Per-run spending limits. See `app/runtime/budget.py`.
    #
    # All four default to 0 = unlimited, which is what a single-operator dev box
    # wants and is exactly the previous behaviour. They matter for anything
    # shared: a loop with a model call inside it spends money on every pass, and
    # `recursion_limit` bounds steps rather than spend.
    max_run_tokens: int = Field(
        default=0, description="Stop a run after this many tokens. 0 = no limit."
    )
    max_run_cost: float = Field(
        default=0.0,
        description=(
            "Stop a run after this much spend. Needs the prices below, or it "
            "silently never fires. 0 = no limit."
        ),
    )
    max_run_seconds: float = Field(
        default=0.0, description="Stop a run after this many seconds. 0 = no limit."
    )
    max_run_steps: int = Field(
        default=0, description="Stop a run after this many node executions. 0 = no limit."
    )
    # What happens when a limit is reached.
    #
    # `error` fails the run, which is the honest default: a stopped run did not
    # complete, and a caller that treats its result as an answer would be wrong.
    #
    # `keep` also fails the run, but carries **whatever the graph had produced so
    # far** in `outputs`. For a long agent that is often worth more than nothing —
    # the point of stopping is to avoid more spend, not to discard the spend that
    # already happened. The status stays `failed`, because it is.
    budget_exit_behavior: str = Field(
        default="error",
        description=(
            "'error' (fail with nothing) or 'keep' (fail, but report the partial "
            "result). Both fail the run; they differ in what they preserve."
        ),
    )
    # What to do when a run is *projected* to overrun, before it actually does.
    #
    # Distinct from `budget_exit_behavior` above, which is about what survives a
    # run that has already gone too far. This one is about the run that can still
    # be steered: `warn` says so and carries on, `stop` finishes the node and ends
    # with its output intact. `warn` is the default because a forecast is an
    # extrapolation, and ending a run on one would be a guess.
    budget_wind_down: str = Field(
        default="warn",
        description=(
            "'warn' (log it and continue) or 'stop' (finish this node, then end "
            "the run keeping its output). Only applies when a limit is configured."
        ),
    )
    #: Prices per 1M tokens, for `max_run_cost`. Optional: only a deployment that
    #: knows its own pricing can turn spend into a number.
    input_price_per_million: float = Field(
        default=0.0, description="Input price per 1M tokens, for the cost limit."
    )
    output_price_per_million: float = Field(
        default=0.0, description="Output price per 1M tokens, for the cost limit."
    )
    # Recorded in an exported agent's `agent.lock`, and reported by its
    # `selfcheck`. Bumped when the runtime changes in a way an agent should know
    # about; it is the human-readable half of "which foundation is this".
    runtime_version: str = Field(
        default="0.1.0", description="Version stamped into exported agent packages."
    )
    # How long a candidate tool module may take to import before the checker gives
    # up. Import-time work is the usual reason a tool module hangs, and a request
    # that never returns is worse than one that says why.
    tool_check_seconds: float = Field(
        default=20.0, description="Timeout for checking a tool module written in the canvas."
    )
    #: Where exported agent packages are written. Under the data directory so a
    #: deployment's exports sit with the rest of its state and can be backed up
    #: in one place.
    agent_export_dir: Path | None = Field(
        default=None,
        description="Directory for exported agent packages. Default: <data_dir>/exports.",
    )
    # Which implementation of the code-execution capability a deployment gets.
    # See `app/runtime/runner.py`. `local` is a resource-limited child process on
    # this host; `container` runs each snippet in a throwaway Docker container
    # with no network and a read-only filesystem.
    sandbox_runner: str = Field(
        default="local",
        description=(
            "'local' (child process on this host) or 'container' (Docker). "
            "Container is the profile for untrusted or multi-tenant deployments; "
            "it fails closed rather than falling back to local execution."
        ),
    )
    # Where a run's history is recorded. See `app/runtime/runs.py`.
    #
    # `sqlite` is what the platform wants: a durable record the UI can read back.
    # `memory` keeps it in this process only — tests, and anything that wants
    # history without a database. `null` records nothing, which is what an
    # exported agent gets: it has no reader for a run history, and shipping a
    # database to it would mean the agent was never really standalone.
    #
    # An unrecognised value is a startup error rather than a fallback, matching
    # SANDBOX_RUNNER — see `RunStoreConfigError`.
    run_store: str = Field(
        default="sqlite",
        description=(
            "'sqlite' (durable, the platform default), 'memory' (this process "
            "only), or 'null' (record nothing - what an exported agent uses)."
        ),
    )
    # Whether scheduled runs are polled for at all. Off is a supported
    # configuration rather than a degraded one — a single-user install that never
    # schedules anything should not have a background task querying the database
    # once every fifteen seconds forever, and a deployment that moves scheduling
    # to an external runner needs this off to avoid two schedulers racing.
    scheduler_enabled: bool = Field(
        default=True,
        description=(
            "Poll for due schedules. Turn off when scheduling is handled "
            "elsewhere, or when nothing is scheduled."
        ),
    )
    # How often the loop looks. This is the **upper bound on lateness**, not a
    # precision setting: a fire due at 09:00 starts within this many seconds of
    # 09:00. Fifteen seconds rather than one, because a one-second tick would run
    # 86,400 queries a day to catch an event whose resolution is a minute.
    scheduler_interval_seconds: float = Field(
        default=15.0,
        description=(
            "How often the scheduler checks for due runs. Bounds how late a run "
            "can be, not how precisely a rule is evaluated."
        ),
    )
    # How late a fire may be before it counts as *missed* rather than merely
    # delayed. Clamped up to `scheduler_interval_seconds` at runtime, because a
    # grace shorter than the polling period would classify every normal cycle as
    # a miss — see `Scheduler.__init__`.
    scheduler_grace_seconds: float = Field(
        default=60.0,
        description=(
            "How late a due run may be before it is recorded as missed. "
            "Raised to the check interval if set below it."
        ),
    )
    # What to do about fires that elapsed while the platform was down.
    #
    # `skip` is the default because the alternative multiplies load exactly when
    # the platform is least able to take it. Either way the missed occurrences are
    # **recorded** — this decides whether to run the backlog, never whether to
    # admit it was missed.
    missed_run_policy: str = Field(
        default="skip",
        description=(
            "'skip' (record the misses, do not run them) or 'catch_up' (run "
            "every missed occurrence on the next tick)."
        ),
    )
    # Whether `redact` should also scan **string contents**, not just key names.
    #
    # Key-name masking cannot see a credential that was templated into a prompt,
    # so it reached step payloads and the transcript intact. The content pass
    # closes that with prefixed patterns (`sk-`, `AKIA`, `ghp_`, `xoxb-`, JWT
    # headers), where a false positive is essentially impossible.
    #
    # On by default: the leak it closes was a documented limitation, and a
    # redactor that is off by default is one nobody notices is off.
    redact_content: bool = Field(
        default=True,
        description=(
            "Also mask secret-shaped strings inside values, not only values under "
            "secret-named keys."
        ),
    )
    # Where a paused run's resume state lives. See `app/runtime/checkpoints.py`.
    #
    # `sqlite` is the default because it is what makes a paused run mean
    # something: a run stopped for a human is only resumable while LangGraph can
    # still read its checkpoints, and the per-run in-memory saver (`memory`) loses
    # them on restart. "Pause, go home, resume tomorrow" is a real workflow, and it
    # silently did not work.
    #
    # Cost, stated plainly: one more file under `data/`, and every run writes its
    # checkpoints there instead of into memory. Eviction deletes them when a run
    # reaches a terminal state, so the file does not grow with finished runs —
    # only with runs that are still waiting on a human, which is the same set the
    # eviction policy deliberately keeps.
    #
    # `memory` remains available and is byte-for-byte the old behaviour. An
    # unrecognised value is a startup error, not a fallback.
    checkpoint_store: str = Field(
        default="sqlite",
        description=(
            "'sqlite' (on disk, so a paused run survives a restart) or 'memory' "
            "(per-run, lost on restart)."
        ),
    )
    sandbox_mode: str = Field(
        default="",
        description=(
            "Local runner only: 'subprocess' (default) or 'inprocess'. The "
            "in-process mode is a development affordance and is not a boundary."
        ),
    )
    sandbox_memory_bytes: int = Field(
        default=0,
        description=(
            "Memory ceiling per snippet. 0 uses the built-in default (512 MB), "
            "which is also what SANDBOX_MEMORY_BYTES did before this setting."
        ),
    )
    sandbox_image: str = Field(
        default="agent-scaffold-sandbox:latest",
        description="Container runner only. Built from docker/sandbox.Dockerfile.",
    )
    # Where an agent's long-term memory lives. See `app/memory/store.py`.
    #
    # `sqlite` is the default because memory that dies with the process is not
    # memory, and sqlite is a file — no server to run, and it was measured to
    # support both cross-restart reads and the vector index that makes semantic
    # search work. `memory` keeps everything in this process; `off` makes memory
    # nodes no-ops so a graph containing one still runs.
    memory_store: str = Field(
        default="sqlite",
        description=(
            "'sqlite' (on disk, so memory survives a restart), 'memory' (this "
            "process) or 'off' (memory nodes do nothing)."
        ),
    )
    # An empty model means "no semantic search": memory still stores and reads,
    # but `search` matches literally. That is a supported configuration rather
    # than a degraded one, and it is the default so nothing reaches the network
    # unless an operator asks for it.
    # Task 20. The memory node is the *explicit* half — the graph says "remember
    # this". This is the automatic half: a successful run records its own output
    # so a later run can recall it. Off by default because every run writing a
    # row is a decision with a cost: the store grows, and near-duplicates make
    # retrieval worse. See `RunManager._remember_if_asked`.
    memory_auto_write: bool = Field(
        default=False,
        description=(
            "When true, a successful run writes its output into long-term "
            "memory automatically. Off by default."
        ),
    )
    embedding_model: str = Field(
        default="",
        description=(
            "Embedding model for semantic memory search, e.g. 'embedding-3' or "
            "'text-embedding-3-small'. Empty disables semantic search."
        ),
    )
    embedding_base_url: str = Field(
        default="",
        description=(
            "Embedding endpoint. Empty uses LLM_BASE_URL, which is right when "
            "both come from the same provider."
        ),
    )
    embedding_api_key: str = Field(
        default="",
        description=(
            "Embedding key. Empty falls back to LLM_API_KEY, for the same "
            "reason: one provider, one credential."
        ),
    )
    embedding_dims: int = Field(
        default=0,
        description=(
            "Vector dimensions. 0 uses the model's default. Must match the "
            "model: GLM's embedding-3 is 2048 by default and allows 256/512/"
            "1024/2048."
        ),
    )
    # `NoDecode` stops pydantic-settings from JSON-parsing the raw env value, so
    # a plain comma-separated string works: CORS_ORIGINS=http://a,http://b
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # ------------------------------------------------------------- Egress ---
    # Outbound requests (the `http_request` tool, and anything added later) are
    # filtered through `runtime.egress`. The default refuses private, loopback,
    # and link-local addresses, which is what stops a workflow from reaching
    # cloud metadata (169.254.169.254) or scanning the LAN.
    egress_allow_private: bool = Field(
        default=False,
        description=(
            "Allow outbound requests to private/loopback addresses. Needed only "
            "when the workflow must call a service on the same machine or LAN; "
            "it re-opens SSRF, so prefer EGRESS_ALLOWED_HOSTS."
        ),
    )
    egress_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Hosts exempt from the private-address block (comma-separated). "
            "Use `host:port` to open a single local service, e.g. "
            "`127.0.0.1:8101`; a bare host opens every port on it."
        ),
    )
    egress_max_redirects: int = Field(
        default=5,
        description="Redirect hops to follow before giving up. Each hop is re-checked.",
    )
    egress_max_response_bytes: int = Field(
        default=2_000_000,
        description="Response body cap. A JSON response is no longer read unbounded.",
    )

    # --------------------------------------------------------------- Tools ---
    # Which tools an agent may call. See `app/tools/registry.py` for why this is
    # configuration rather than a hard-coded list.
    #
    # TOOLS_ALLOWLIST empty means "every registered tool", which keeps a
    # general-purpose agent zero-configuration. Non-empty bounds the agent to
    # exactly those names — the form an exported agent's manifest takes.
    # TOOLS_DENYLIST is subtracted afterwards, for switching off one capability
    # (e.g. HTTP_REQUEST on a host with no network) without enumerating the rest.
    tools_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    tools_denylist: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --------------------------------------------------------------- Auth ---
    # See `app/security/auth.py`. The governing rule is that authentication may
    # be *requested* early but never waived by accident: `local` is honoured only
    # when the server is bound to a loopback address.
    server_host: str = Field(
        default="127.0.0.1",
        description=(
            "Address the server is bound to. Must match the real bind address — "
            "it is what decides whether AUTH_MODE=local is permitted."
        ),
    )
    auth_mode: str = Field(
        default="local",
        description=(
            "'local' (no token, loopback only) or 'token' (bearer required). "
            "Ignored — escalated to 'token' — if the bind is not loopback."
        ),
    )
    # Entries are 'name:token' or 'name:token:scope|scope'. Scopes: read, write,
    # admin. Stored hashed in memory; the plaintext only ever lives in this field.
    auth_tokens: Annotated[list[str], NoDecode] = Field(default_factory=list)
    auth_salt: str = Field(
        default="",
        description=(
            "Salt for token hashing. Leave blank for a stable per-install default; "
            "set it to rotate every token at once."
        ),
    )
    secrets_file: Path | str = Field(
        default="",
        description=(
            "JSON file of secret name -> value, e.g. {'llm.api_key': 'sk-...'}. "
            "When set it WINS over .env, which is what lets an exported agent "
            "carry its own credentials. See app/security/secrets.py."
        ),
    )

    # --------------------------------------------------------------- AI -----
    ai_enabled: bool = Field(default=True)
    ai_max_tool_iterations: int = Field(
        default=30,
        description="Max tool-calling rounds when generating a graph from a prompt.",
    )
    ai_self_test: bool = Field(
        default=True,
        description=(
            "Trial-run a freshly generated graph before returning it, so runtime "
            "mistakes reach the generator's correction loop instead of the user."
        ),
    )
    ai_self_test_timeout_seconds: float = Field(
        default=180,
        ge=10,
        le=900,
        description="How long one generated-graph trial run may take.",
    )
    ai_copilot_max_tool_iterations: int = Field(
        default=24,
        ge=4,
        le=100,
        description=(
            "Max tool-calling rounds for one copilot edit. The copilot has to "
            "understand, edit and then correct runtime failures, so it gets a "
            "little more room than a repair loop."
        ),
    )
    ai_business_research: bool = Field(
        default=True,
        description=(
            "Let the copilot research industry practice when its business brief "
            "says the request is too vague to decide a direction. The stage is "
            "always shown; disabling it only skips the web search."
        ),
    )
    ai_business_research_max_queries: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Search queries allowed for one business-insight stage.",
    )

    # ------------------------------------------------------- Public access ---
    # Public mode is deliberately a separate switch from development mode. With
    # `PUBLIC_ACCESS=false` every new middleware is a no-op, so a laptop run is
    # byte-for-byte the old behaviour. Turning it on requires a loopback bind, a
    # signed gate cookie, and (for the demo profile) Code nodes off.
    public_access: bool = Field(
        default=False,
        description=(
            "Expose the app through a reverse tunnel with a human-verification "
            "gate. Requires AUTH_MODE=local and a loopback SERVER_HOST."
        ),
    )
    public_demo: bool = Field(
        default=False,
        description=(
            "Restrict a PUBLIC_ACCESS deployment to a safe demo: no Code nodes, "
            "no custom tools, no scheduler, admin-only destructive endpoints."
        ),
    )
    public_origin: str = Field(
        default="",
        description=(
            "Canonical public origin, e.g. https://demo.example.com. Used for "
            "Origin checks, the secure cookie decision, and Turnstile hostname "
            "verification."
        ),
    )
    frontend_dist: Path = Field(
        default=PROJECT_ROOT / "frontend" / "dist",
        description="Built SPA directory served by the backend in public mode.",
    )
    trusted_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Additional Host header values accepted in public mode. The public "
            "origin host and loopback names are always accepted."
        ),
    )
    gate_cookie_secret: str = Field(
        default="",
        description=(
            "HMAC secret for the human-verification cookie and challenge. Empty "
            "falls back to AUTH_SALT; public mode requires at least 32 characters."
        ),
    )
    gate_cookie_name: str = Field(default="scaffold_gate")
    gate_cookie_ttl_seconds: int = Field(default=43_200, ge=60, le=604_800)
    gate_cookie_secure: bool = Field(
        default=True,
        description="Set the Secure flag on the gate cookie. Keep true for HTTPS.",
    )
    visitor_cookie_name: str = Field(default="scaffold_visitor")
    visitor_cookie_ttl_seconds: int = Field(
        default=2_592_000,
        ge=3_600,
        le=31_536_000,
        description=(
            "Anonymous visitor id lifetime. It is a quota key, not a login: it "
            "never identifies a person and carries no scopes."
        ),
    )
    gate_challenge_ttl_seconds: int = Field(default=300, ge=30, le=1_800)
    gate_pow_difficulty: int = Field(
        default=16,
        ge=12,
        le=22,
        description="Required SHA-256 leading zero bits for the fallback challenge.",
    )
    gate_bypass_token: str = Field(
        default="",
        description=(
            "High-value header token for smoke/verification scripts. Never send "
            "it to the browser. Empty disables the bypass."
        ),
    )
    admin_token: str = Field(
        default="",
        description=(
            "Header token for owner-only endpoints in public mode "
            "(X-Admin-Token). Never send it to the browser."
        ),
    )
    turnstile_site_key: str = Field(default="")
    turnstile_secret_key: str = Field(default="")
    turnstile_verify_url: str = Field(
        default="https://challenges.cloudflare.com/turnstile/v0/siteverify"
    )
    turnstile_timeout_seconds: float = Field(default=5.0, ge=1.0, le=20.0)
    max_request_bytes: int = Field(
        default=0,
        description="Reject request bodies larger than this in public mode. 0 disables.",
    )
    hsts_max_age: int = Field(default=15_552_000, ge=0)
    rate_limit_access_per_minute: int = Field(default=10, ge=1)
    rate_limit_verify_per_minute: int = Field(default=5, ge=1)
    rate_limit_general_per_minute: int = Field(default=120, ge=1)
    rate_limit_ai_per_minute: int = Field(default=10, ge=1)
    rate_limit_ai_per_day_global: int = Field(default=200, ge=1)
    rate_limit_ai_per_day_per_visitor: int = Field(
        default=3, ge=1, description="AI authoring requests per anonymous visitor per day."
    )
    rate_limit_ai_per_day_per_ip: int = Field(
        default=10, ge=1, description="AI authoring requests per IP (/64 for IPv6) per day."
    )
    rate_limit_runs_per_minute: int = Field(default=20, ge=1)
    rate_limit_runs_per_day_per_ip: int = Field(default=50, ge=1)
    rate_limit_runs_per_day_per_visitor: int = Field(
        default=2, ge=1, description="Runs per anonymous visitor per day."
    )
    rate_limit_hooks_per_minute: int = Field(default=60, ge=1)
    #: Token allowances are separate from request counts because two requests can
    #: differ by orders of magnitude. 0 disables the corresponding token limit.
    visitor_daily_tokens: int = Field(
        default=50_000, ge=0, description="Token budget per anonymous visitor per day."
    )
    ip_daily_tokens: int = Field(
        default=200_000, ge=0, description="Token budget per IP per day."
    )
    global_daily_tokens: int = Field(
        default=2_000_000, ge=0, description="Token budget for the whole public demo per day."
    )
    ai_request_token_limit: int = Field(
        default=30_000,
        ge=0,
        description=(
            "Stop one AI authoring request after this many model tokens. Runs use "
            "MAX_RUN_TOKENS instead."
        ),
    )
    max_workflows: int = Field(
        default=0, description="Maximum saved workflows in public demo. 0 disables."
    )
    max_nodes_per_workflow: int = Field(
        default=0, description="Maximum nodes per graph in public demo. 0 disables."
    )
    max_edges_per_workflow: int = Field(
        default=0, description="Maximum edges per graph in public demo. 0 disables."
    )

    # ------------------------------------------------------------ methods ---
    @field_validator(
        "cors_origins",
        "auth_tokens",
        "egress_allowed_hosts",
        "tools_allowlist",
        "tools_denylist",
        "trusted_hosts",
        mode="before",
    )
    @classmethod
    def _split_list(cls, value: object) -> object:
        """Accept a comma-separated string so `.env` stays readable.

        One validator for every list field, because they are all configured the
        same way and three near-identical copies is three places for the parsing
        to drift apart.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("llm_thinking", mode="before")
    @classmethod
    def _validate_llm_thinking(cls, value: object) -> str:
        text = str(value or "auto").strip().lower()
        if text not in {"auto", "enabled", "disabled"}:
            raise ValueError(
                "LLM_THINKING must be auto, enabled, or disabled "
                f"(got {value!r})."
            )
        return text

    @field_validator("llm_reasoning_effort", mode="before")
    @classmethod
    def _validate_llm_reasoning_effort(cls, value: object) -> str:
        text = str(value or "high").strip().lower()
        if text not in {"low", "high", "max"}:
            raise ValueError(
                "LLM_REASONING_EFFORT must be low, high, or max "
                f"(got {value!r})."
            )
        return text

    @field_validator("auth_mode", mode="before")
    @classmethod
    def _normalise_mode(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("data_dir", "frontend_dist", mode="before")
    @classmethod
    def _expand_path(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(os.path.expandvars(value)).expanduser()
        return value

    @property
    def resolved_gate_secret(self) -> str:
        """The HMAC secret for the gate, with the auth salt as a dev fallback."""
        return self.gate_cookie_secret or self.auth_salt or "agent-scaffold-dev-gate"

    @property
    def public_origin_host(self) -> str:
        try:
            return (urlsplit(self.public_origin).hostname or "").lower()
        except ValueError:
            return ""

    @property
    def public_origin_is_https(self) -> bool:
        return self.public_origin.lower().startswith("https://")

    @property
    def resolved_trusted_hosts(self) -> set[str]:
        hosts = {host.lower().strip("[]") for host in self.trusted_hosts if host.strip()}
        hosts.update({"localhost", "127.0.0.1", "::1"})
        if self.public_origin_host:
            hosts.add(self.public_origin_host)
        return hosts

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def validate_public(self) -> None:
        """Fail closed when a public deployment is misconfigured.

        Called from the lifespan, before the first request. The point is that a
        public deployment cannot start with a non-loopback bind, no gate secret,
        half-configured Turnstile, or (in demo mode) Code nodes and a scheduler
        still enabled. A warning in a log file is not enough for those.
        """
        if not self.public_access:
            if self.public_demo:
                raise ValueError("PUBLIC_DEMO requires PUBLIC_ACCESS=true.")
            return

        if self.auth_mode != "local":
            raise ValueError(
                "PUBLIC_ACCESS requires AUTH_MODE=local. The tunnel terminates on "
                "loopback; token mode would reject the browser before the gate."
            )
        if self.server_host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError(
                "PUBLIC_ACCESS requires SERVER_HOST to be a loopback address. "
                "Never bind the public backend to 0.0.0.0."
            )

        if not self.public_origin:
            raise ValueError("PUBLIC_ACCESS requires PUBLIC_ORIGIN, e.g. https://demo.example.com.")
        parsed = urlsplit(self.public_origin)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("PUBLIC_ORIGIN must be an absolute http(s) URL.")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("PUBLIC_ORIGIN must not include a path, query, or fragment.")
        if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError(
                "PUBLIC_ORIGIN must use https outside localhost. Cloudflare Tunnel "
                "provides TLS at the edge."
            )

        if len(self.resolved_gate_secret) < 32:
            raise ValueError(
                "GATE_COOKIE_SECRET (or AUTH_SALT) must be at least 32 characters "
                "in public mode."
            )
        for name, value in (
            ("AUTH_SALT", self.auth_salt),
            ("GATE_COOKIE_SECRET", self.gate_cookie_secret),
            ("ADMIN_TOKEN", self.admin_token),
        ):
            if "replace-with" in value:
                raise ValueError(
                    f"{name} still contains the example placeholder. Generate a "
                    "real random value before starting public mode."
                )

        if bool(self.turnstile_site_key) != bool(self.turnstile_secret_key):
            raise ValueError(
                "TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY must be set together "
                "or both left empty (PoW fallback only)."
            )

        if not self.admin_token or len(self.admin_token) < 32:
            raise ValueError(
                "PUBLIC_ACCESS requires ADMIN_TOKEN with at least 32 characters. "
                "It is the owner's only management credential in public mode."
            )
        if self.gate_bypass_token and len(self.gate_bypass_token) < 32:
            raise ValueError("GATE_BYPASS_TOKEN must be at least 32 characters when set.")

        if self.public_demo:
            if self.code_node_enabled:
                raise ValueError(
                    "PUBLIC_DEMO requires CODE_NODE_ENABLED=false. The local "
                    "sandbox does not isolate the filesystem or network."
                )
            if self.scheduler_enabled:
                raise ValueError("PUBLIC_DEMO requires SCHEDULER_ENABLED=false.")
            if self.max_concurrent_runs <= 0:
                raise ValueError("PUBLIC_DEMO requires MAX_CONCURRENT_RUNS > 0.")
            if min(self.max_run_tokens, self.max_run_seconds, self.max_run_steps) <= 0:
                raise ValueError(
                    "PUBLIC_DEMO requires MAX_RUN_TOKENS, MAX_RUN_SECONDS, and "
                    "MAX_RUN_STEPS to all be greater than zero."
                )

        index = self.frontend_dist / "index.html"
        if not index.is_file():
            raise ValueError(
                f"PUBLIC_ACCESS requires a built frontend at {index}. Run "
                "scripts/deploy/build_public.ps1 first."
            )

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'scaffold.db').as_posix()}"

    @property
    def workflows_dir(self) -> Path:
        """Git-friendly JSON exports of every workflow."""
        return self.data_dir / "workflows"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def resolved_agent_export_dir(self) -> Path:
        """Where exported agent packages go.

        Under the data directory by default, so a deployment's exports sit with
        the rest of its state and one backup covers both.
        """
        return self.agent_export_dir or (self.data_dir / "exports")

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.workflows_dir, self.runs_dir, self.logs_dir, self.backups_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
