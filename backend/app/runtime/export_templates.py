"""The files an exported agent gets, as text.

Separated from `export.py` for one reason that showed up immediately: the
generated `tools/__init__.py` contains a worked example, which means it contains
a module docstring, a function, `ToolDef(...)` and JSON — four levels of quoting
if it is written inline. Written inline the exporter produced a file that did not
parse at all, and only the export test (which runs the package in a subprocess)
caught it.

Here each file is one plain string literal. No interpolation happens inside the
examples, so nothing has to be escaped.

`main.py` is the only generated file that must agree with the runtime's API, so it
imports from the vendored copy at the top of the file and does nothing clever: the
fewer moving parts, the less there is to drift.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# tools/__init__.py
# ---------------------------------------------------------------------------

TOOLS_INIT = '''"""This agent's own tools.

Importing a module is what registers its tools. There is no separate registration
step, because a tool that sits in this directory but is never imported is the
mistake that looks like the model refusing to call it.

Credentials for a business system do not belong in this package. Read them from
the environment (`SECRETS_FILE`, or any variable): the exporter strips the
credentials it finds in the workflow definition, and a key committed here would
defeat that.

A worked example lives in `tools/lookup_order.py.example` — copy it to
`lookup_order.py`, then add `from . import lookup_order` below.
"""

from __future__ import annotations

# from . import lookup_order  # noqa: F401  - importing registers it
'''

#: The example, as a *separate file* rather than a docstring.
#:
#: It is a real, importable module: copying it to `lookup_order.py` and adding one
#: import line is a complete, working tool. That is worth more than prose about
#: how to write one, and it means the example is a file the customer's editor and
#: linter both understand.
TOOLS_EXAMPLE = '''"""Example business tool. Copy to `lookup_order.py` and import it.

A tool is an async callable plus an OpenAI-style JSON schema. The schema does
double duty: the model receives it (so descriptions are written for the model to
read, not for a human), and the Tool node validates arguments against it.
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import ToolDef, register_tool


async def _lookup_order(order_id: str) -> dict[str, Any]:
    """Look one order up in the business system.

    A real implementation calls the customer's API here. Two things worth doing:

    * Send outbound requests through `app.runtime.egress.get_egress_policy()` —
      the same filter the HTTP Request tool uses — so a host this deployment is
      not allowed to reach is refused rather than contacted.
    * Read the endpoint and any credential from the environment, not from this
      file. `config/.env.example` lists what has to be set.
    """
    return {"order_id": order_id, "status": "unknown"}


register_tool(
    ToolDef(
        name="lookup_order",
        label="Look up order",
        label_zh="查询订单",
        # Written for the model: say when to use it, not just what it does.
        description="Look up one order by its id. Use it when the user asks about a specific order.",
        description_zh="按订单号查询一个订单。用户问到具体某个订单时使用。",
        parameters={
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order's id, as shown to the user.",
                }
            },
            "required": ["order_id"],
        },
        fn=_lookup_order,
        # True marks a tool that reaches outside the process; the UI and the
        # registry treat those differently.
        dangerous=False,
    )
)
'''

# ---------------------------------------------------------------------------
# main.py
# ---------------------------------------------------------------------------

MAIN = '''"""Entry point for this agent.

    python main.py run --input '{"query": "..."}'   Run once, print the result
    python main.py daemon --interval 60             Run continuously, one cycle per interval
    python main.py selfcheck                        Verify the package is intact
    python main.py doctor                           Check capability packs and dependencies
    python main.py serve                            JSON lines on stdin -> results
    python main.py serve --http --port 8080         HTTP API + chat page (sessions, files)

Nothing here reaches the canvas or its database. The runtime is vendored under
`vendor/runtime`, so what this file needs is what is in this directory.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys
import time
import uuid

HERE = pathlib.Path(__file__).resolve().parent

# The runtime is vendored, and it imports itself as `app.*` — so this directory is
# what goes on the path, not `vendor/runtime/app`.
sys.path.insert(0, str(HERE / "vendor" / "runtime"))
sys.path.insert(0, str(HERE))


def load_agent():
    """The definition, with `$ref` references expanded from their files.

    Prompts and code were split into `prompts/*.md` and `code/*.py` at export so
    they are ordinary files an editor understands. They have to be folded back
    in here, because the runtime consumes one definition — it should not know or
    care that the author's copy is spread across a directory.
    """
    import yaml

    from app.graph.schema import WorkflowGraph

    # `!ref prompts/extract.md` becomes that file's text. A SafeLoader subclass
    # rather than a patched global, so nothing else in this process picks the tag
    # up by accident.
    class _Loader(yaml.SafeLoader):
        pass

    def _ref(loader, node):
        path = loader.construct_scalar(node)
        target = HERE / path
        if not target.exists():
            raise SystemExit(f"agent.yaml references '{path}', which is missing")
        return target.read_text(encoding="utf-8")

    _Loader.add_constructor("!ref", _ref)

    with open(HERE / "agent.yaml", encoding="utf-8") as handle:
        payload = yaml.load(handle, Loader=_Loader)
    return WorkflowGraph.model_validate(payload)


_MANIFEST: dict | None = None


def load_manifest() -> dict:
    """This package's capability declaration.

    Written by the exporter and not edited at run time: it names the packs the
    agent may load, the credentials it expects, and the chat entry keys. Read
    lazily so a hand-broken manifest is reported by `doctor` rather than killing
    the process on import.
    """
    global _MANIFEST
    if _MANIFEST is None:
        import yaml

        path = HERE / "manifest.yml"
        try:
            _MANIFEST = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            _MANIFEST = {}
        except Exception as exc:  # noqa: BLE001 - doctor/selfcheck report it
            print(f"WARNING: manifest.yml could not be read: {exc}", file=sys.stderr)
            _MANIFEST = {}
    return _MANIFEST


def agent_name() -> str:
    """The package's name, for the chat page and the health answer."""
    name = load_manifest().get("name")
    return str(name) if name else "agent"


_PREPARED = False


def _referenced_tools() -> set[str]:
    """Tool names the definition actually uses.

    Used to decide which runtime built-ins this agent gets. The manifest lists
    them too, but reading the graph is the source of truth: a hand-edited
    `agent.yaml` must not end up with a registry that disagrees with its nodes.
    """
    try:
        graph = load_agent()
    except Exception:  # noqa: BLE001 - a broken graph is reported by compile/run
        return set()
    names: set[str] = set()
    for node in graph.nodes:
        config = node.config or {}
        if node.type == "tool":
            name = config.get("tool")
            if name:
                names.add(str(name))
        elif node.type == "agent":
            requested = config.get("tools")
            if isinstance(requested, list):
                names.update(str(name) for name in requested if name)
    return names


def prepare() -> None:
    """Apply this package's configuration before anything reads it.

    Runs once per process. The daemon calls `run_once` on every tick; re-running
    the tool import on each tick would register the package's tools a second time
    and `BuiltinToolRegistry.register` rejects duplicate names — the first fix
    for the empty-registry bug would then have broken every agent with custom
    tools on its second cycle.
    """
    global _PREPARED
    if _PREPARED:
        return

    # Set before the runtime's settings are read, so a deployment that configures
    # nothing still gets a working agent rather than one that reaches for a
    # platform database it does not have.
    os.environ.setdefault("RUN_STORE", "null")
    os.environ.setdefault("CHECKPOINT_STORE", "memory")
    os.environ.setdefault("TASK_QUEUE", "memory")
    os.environ.setdefault("SANDBOX_RUNNER", "local")
    # The package owns its data, not the directory the runtime happens to be
    # vendored in. `db_write`/`db_query` resolve `results.db` under this path.
    os.environ.setdefault("DATA_DIR", str(HERE / "data"))
    # The files capability pack reads and writes here; the HTTP upload endpoint
    # writes to the same root. One directory is what makes "upload, then ask
    # about the file" two requests instead of two unrelated features.
    workspace_dir = HERE / "workspace"
    os.environ.setdefault("AGENT_WORKSPACE_DIR", str(workspace_dir))
    workspace_dir.mkdir(parents=True, exist_ok=True)

    secrets_file = os.environ.get("SECRETS_FILE") or str(HERE / "config" / "secrets.env")
    if pathlib.Path(secrets_file).exists():
        os.environ["SECRETS_FILE"] = secrets_file

    # `config/secrets.env` as plain KEY=VALUE, so a deployment that copied the
    # shipped example works without exporting variables by hand. This is the
    # package's own file, not the platform's `.env`: the runtime's Settings
    # resolves env files next to the *vendored* runtime, and a customer should
    # not have to know that. Existing environment variables always win.
    inline_env = pathlib.Path(
        os.environ.get("SECRETS_ENV_FILE") or str(HERE / "config" / "secrets.env")
    )
    if inline_env.exists():
        try:
            for raw_line in inline_env.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                if key:
                    os.environ.setdefault(key, value.strip())
        except OSError as exc:  # noqa: BLE001 - a bad file is reported by doctor
            print(f"WARNING: could not read {inline_env}: {exc}", file=sys.stderr)

    # This agent's own tools, loaded from its own directory into its own registry.
    # Two consequences that are the point: another agent's tools are not visible
    # here, and a plugin installed on this host cannot widen this agent.
    from app.tools.loading import ToolLoadError, load_tools_from
    from app.tools.registry import set_tool_registry

    tool_dir = HERE / "tools"
    if tool_dir.is_dir():
        try:
            # The agent's own tools first, then exactly the capability-pack tools
            # its manifest declares and its graph references. Starting from an
            # empty registry made every Tool node for a built-in fail with
            # "unknown tool"; starting from *every* registered tool would hand
            # the agent capabilities it never declared.
            registry, problems = load_tools_from(tool_dir)
            problems = list(problems) + _add_pack_tools(registry)
        except ToolLoadError as exc:
            # Loud on purpose. A tool that failed to import means this agent is
            # not the one that was built, and starting anyway would present it as
            # "the model will not call that tool".
            print(f"FATAL: {exc}", file=sys.stderr)
            raise SystemExit(2)
        for problem in problems:
            # Per-tool complaints do not stop the agent: a malformed schema costs
            # you that tool, not the deployment.
            print(f"WARNING: {problem}", file=sys.stderr)
        set_tool_registry(registry)

    _PREPARED = True


def _add_pack_tools(registry) -> list[str]:
    """Register the capability packs this package declares.

    The manifest names the packs; the graph names the tools. Both are required:
    a hand-edited `agent.yaml` must not gain a tool the package never declared,
    and installing the runtime on a machine must not, by itself, widen any agent.

    A package exported before packs existed has no `packs:` key. It falls back to
    the previous behaviour (the built-ins its graph names) so an old export keeps
    running after a runtime upgrade.

    Returns problems that should not be fatal -- unknown names are the graph's
    problem, and compile/run reports them; failing to start here would turn a
    typo into a package that cannot even print its tool list.
    """
    referenced = _referenced_tools()
    declared = load_manifest().get("packs")
    if not isinstance(declared, list):
        from app.tools.registry import BuiltinToolRegistry, ToolError

        builtins = BuiltinToolRegistry()
        for name in sorted(referenced):
            if name in registry:
                continue
            try:
                registry.register(builtins.get(name))
            except ToolError:
                pass
        return []

    from app.runtime.packs.registry import builtin_registry

    try:
        builtin_registry().register_tools(
            registry,
            packs=[str(name) for name in declared if name],
            tool_names=sorted(referenced),
            strict=False,
        )
    except Exception as exc:  # noqa: BLE001 - a pack that cannot load is fatal
        # Loud on purpose: an agent that is missing a tool it was built with
        # fails later as "the model will not call that tool", which is the
        # wrong place to debug it.
        print(
            f"FATAL: capability packs failed to load: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return [
        f"tool '{name}' is referenced by agent.yaml but no declared pack provides it"
        for name in sorted(referenced)
        if name not in registry
    ]


#: One run at a time in this process.
#:
#: The HTTP server is threaded, and the memory/checkpointer providers are
#: process-wide singletons (that is the runtime's injection-point design, not
#: something this file can change). Two overlapping runs would therefore swap
#: each other's provider and one might close a provider the other is still
#: using. A single agent deployment does not need concurrent runs, and
#: serialising is the cheap correct answer; `run` / `serve` (stdin) / `daemon`
#: never contend because they are synchronous.
_RUN_LOCK = asyncio.Lock()


async def run_once_async(inputs: dict, emit=None) -> dict:
    """Run the agent once.

    The session layer and the HTTP server await this directly; `run_once` below
    is the blocking form the CLI subcommands use.
    """
    from app.graph.compiler import compile_graph
    from app.memory import build_memory_store_provider, set_memory_store_provider
    from app.runtime.checkpoints import build_checkpointer_provider, set_checkpointer_provider
    from app.settings import get_settings

    prepare()
    settings = get_settings()
    if emit is not None:
        emit({"type": "run_started", "inputs": sorted(inputs)})

    async with _RUN_LOCK:
        memory_provider = build_memory_store_provider(settings)
        set_memory_store_provider(memory_provider)
        await memory_provider.open()

        checkpoint_provider = build_checkpointer_provider(settings)
        set_checkpointer_provider(checkpoint_provider)
        await checkpoint_provider.open()

        try:
            compiled = compile_graph(load_agent(), checkpointer=checkpoint_provider.make())
            # A fresh thread per run, deliberately. Conversation continuity comes
            # from the session layer's explicit `history` input, not from LangGraph
            # state: a fixed thread id would accumulate every conversation's state
            # in one thread (and in the checkpoint store) until the process died.
            config = {"configurable": {"thread_id": f"run-{uuid.uuid4().hex[:12]}"}}
            result = await compiled.ainvoke({"inputs": inputs}, config=config)
            output = result.get("output") or {}
            if emit is not None:
                emit({"type": "run_finished"})
            return output
        finally:
            await memory_provider.close()
            await checkpoint_provider.close()


def run_once(inputs: dict) -> dict:
    """Blocking wrapper for `run`, `serve` (stdin) and the daemon."""
    return asyncio.run(run_once_async(inputs))


def cmd_run(args: argparse.Namespace) -> int:
    inputs = json.loads(args.input) if args.input else {}
    output = run_once(inputs)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


_SESSION_SERVICE = None


def session_service():
    """The conversation store + service behind `serve --http`.

    One SQLite file under `data/`, so a conversation survives a restart. Built
    lazily: `run`, `serve` (stdin), `daemon`, `doctor` and `selfcheck` never
    create it.
    """
    global _SESSION_SERVICE
    if _SESSION_SERVICE is None:
        from app.runtime.session import SessionConfig, SessionService, SQLiteSessionStore

        data_dir = pathlib.Path(os.environ.get("DATA_DIR") or str(HERE / "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        block = load_manifest().get("session")
        if not isinstance(block, dict):
            block = {}
        config = SessionConfig(
            message_key=os.environ.get("AGENT_MESSAGE_KEY")
            or str(block.get("message_key") or "message"),
            history_key=os.environ.get("AGENT_HISTORY_KEY")
            or str(block.get("history_key") or "history"),
            output_key=os.environ.get("AGENT_OUTPUT_KEY")
            or str(block.get("output_key") or ""),
        )
        store = SQLiteSessionStore(data_dir / "sessions.db")
        _SESSION_SERVICE = SessionService(store, run_once_async, config)
    return _SESSION_SERVICE


def cmd_serve(args: argparse.Namespace) -> int:
    """JSON lines on stdin by default; the HTTP chat API with `--http`."""
    if not getattr(args, "http", False):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                output = run_once(json.loads(line))
                print(json.dumps({"ok": True, "output": output}, ensure_ascii=False), flush=True)
            except Exception as exc:  # noqa: BLE001 - the caller gets the reason
                print(
                    json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                    flush=True,
                )
        return 0

    from app.runtime.http_api import AgentHTTPServer, HTTPConfig
    from app.runtime.workspace import workspace_from_env

    # Before the server answers its first request. `prepare()` is what sets
    # AGENT_WORKSPACE_DIR (and the data dir), so an upload that arrives before
    # the first run must not land in a different directory than the tools read.
    # Measured: without this, `/v1/files` wrote to `<DATA_DIR>/workspaces/default`
    # while the files pack read `<package>/workspace`, so the agent reported
    # "no files in the workspace" right after a successful upload.
    prepare()

    config = HTTPConfig(
        host=args.host,
        port=args.port,
        token=args.token or os.environ.get("AGENT_HTTP_TOKEN", ""),
        agent_name=agent_name(),
    )
    server = AgentHTTPServer(
        run_fn=run_once_async,
        session_service=session_service(),
        workspace_factory=workspace_from_env,
        config=config,
    )
    print(f"{agent_name()} listening on {server.url}", flush=True)
    print("open it in a browser; Ctrl+C stops the server", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            pass
    return 0


#: How the daemon behaves when the same command runs for years.
#:
#: The platform's own continuity comes from a scheduler; an exported package has
#: no platform, so this is that scheduler in miniature. Three rules make it
#: survivable rather than merely repetitive:
#:
#: * **One bounded run per tick.** A bad cycle fails alone; the next tick still
#:   happens, and the previous failure stays in the cycle log.
#: * **State on disk, not in memory.** The daily-report marker and failure count
#:   live in SQLite, so a restart does not re-send yesterday's report or forget
#:   that the gateway has been down for an hour.
#: * **Observable without reading the code.** `health.json` and a JSONL-rotated
#:   `agent.log` are what a supervisor, a dashboard, or a person checks.
#:
#: It is deliberately not a "never-ending graph run": each tick invokes the same
#: bounded graph that `run` invokes once, and ends. That is what keeps a year of
#: operation auditable and cancelable.
_DAEMON_HELP = """Run continuously, one bounded cycle per interval.

The package has no platform scheduler, so this process is the scheduler:

    python main.py daemon --interval 60 \\
        --input '{"line_id":"IM-07","mode":"monitor"}' \\
        --daily-at 08:00 --timezone Asia/Shanghai \\
        --daily-input '{"line_id":"IM-07","mode":"daily_report"}'

State and logs live under `data/` (or `--data-dir`):
    agent_cycles.db   one row per cycle; status, duration, error
    health.json       last status, consecutive failures, next expectation
    agent.log         JSON lines, rotated at 10 MB
    results.db        written by this agent's db_write nodes

Run it under a supervisor that restarts on exit (systemd `Restart=always`,
Docker `--restart unless-stopped`, or a Windows scheduled task). The daemon
handles a failing *cycle*; the supervisor handles a dead *process*.
"""


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _atomic_write(path: pathlib.Path, text: str) -> None:
    """Write via a sibling then replace, so readers never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _open_cycle_db(path: pathlib.Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute(
        "CREATE TABLE IF NOT EXISTS cycles ("
        "id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at TEXT NOT NULL, "
        "finished_at TEXT, status TEXT NOT NULL, error TEXT, duration_ms REAL, "
        "output TEXT)"
    )
    db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    # A row left in `running` means the process died mid-cycle. Mark it rather
    # than leaving a phantom that looks live forever.
    db.execute(
        "UPDATE cycles SET status='interrupted', finished_at=? WHERE status='running'",
        (_iso_now(),),
    )
    db.commit()
    return db


def _prune_cycles(db: sqlite3.Connection, retain_days: int) -> int:
    """Keep the cycle log bounded; a year at one per minute is 525k rows.

    Only the daemon's own audit log is pruned. The agent's `results.db` is
    business data and its retention belongs to the business, not to this
    template. Deleting a cycle row never deletes what the workflow wrote.
    """
    if retain_days <= 0:
        return 0
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retain_days)).isoformat()
    cursor = db.execute(
        "DELETE FROM cycles WHERE status != 'running' AND started_at < ?", (cutoff,)
    )
    db.commit()
    return cursor.rowcount or 0


def _state_get(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _state_set(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()


def _acquire_lock(path: pathlib.Path):
    """Hold an OS file lock for the process lifetime.

    A second daemon on the same data directory would double every setpoint write
    and every email, which is exactly the failure a lock prevents.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        handle.seek(0)
        if handle.read(1) == "":
            handle.write("0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _log(record: dict, data_dir: pathlib.Path) -> None:
    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # A Windows console on a legacy code page is not a reason to lose the
        # cycle. The file below is always UTF-8.
        print(json.dumps(record, ensure_ascii=True, default=str), flush=True)
    try:
        path = data_dir / "agent.log"
        if path.exists() and path.stat().st_size > 10_000_000:
            os.replace(path, data_dir / "agent.log.1")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")
    except OSError:
        # Losing the file copy must not lose the cycle; stdout still has it.
        pass


def _parse_daily_at(value: str) -> int | None:
    """`HH:MM` to minutes since local midnight; empty disables the report."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        hour_text, minute_text = text.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except ValueError:
        raise SystemExit(f"--daily-at must be HH:MM, got {text!r}")
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise SystemExit(f"--daily-at is out of range: {text!r}")
    return hour * 60 + minute


def _local_now(timezone: str) -> dt.datetime:
    if not timezone:
        return dt.datetime.now().astimezone()
    from zoneinfo import ZoneInfo

    return dt.datetime.now(ZoneInfo(timezone))


def _run_cycle(
    db: sqlite3.Connection,
    inputs: dict,
    mode: str,
    data_dir: pathlib.Path,
    interval: float,
    daily_at: str,
) -> dict:
    cycle_id = uuid.uuid4().hex
    started_at = _iso_now()
    started = time.monotonic()
    db.execute(
        "INSERT INTO cycles(id, mode, started_at, status) VALUES(?, ?, ?, 'running')",
        (cycle_id, mode, started_at),
    )
    db.commit()

    status = "succeeded"
    error = ""
    output: dict = {}
    try:
        output = run_once(inputs)
    except Exception as exc:  # noqa: BLE001 - one failed cycle must not stop the loop
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    duration_ms = (time.monotonic() - started) * 1000.0
    finished_at = _iso_now()
    rendered = json.dumps(output, ensure_ascii=False, default=str)
    if len(rendered) > 60_000:
        rendered = rendered[:60_000] + "...(truncated)"
    db.execute(
        "UPDATE cycles SET finished_at=?, status=?, error=?, duration_ms=?, output=? "
        "WHERE id=?",
        (finished_at, status, error, duration_ms, rendered, cycle_id),
    )
    db.commit()

    total = db.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
    failures = 0
    if status == "failed":
        failures = int(_state_get(db, "consecutive_failures", "0")) + 1
        _state_set(db, "consecutive_failures", str(failures))
        _state_set(db, "last_error", error)
    else:
        _state_set(db, "consecutive_failures", "0")
        _state_set(db, "last_success_at", finished_at)

    record = {
        "at": finished_at,
        "cycle_id": cycle_id,
        "mode": mode,
        "status": status,
        "duration_ms": round(duration_ms, 1),
        "error": error,
        "cycles_total": total,
        "consecutive_failures": failures,
    }
    _atomic_write(
        data_dir / "health.json",
        json.dumps(
            {
                **record,
                "pid": os.getpid(),
                "interval_seconds": interval,
                "daily_at": daily_at or None,
                "last_success_at": _state_get(db, "last_success_at", ""),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\\n",
    )
    _log(record, data_dir)
    return record


def cmd_daemon(args: argparse.Namespace) -> int:
    """Run monitor cycles forever (or `--cycles`) with an optional daily report."""
    interval = float(args.interval)
    if interval < 1:
        print("--interval must be at least 1 second", file=sys.stderr)
        return 2
    at_minutes = _parse_daily_at(args.daily_at)
    try:
        daily_input = json.loads(args.daily_input) if args.daily_input else None
        monitor_input = json.loads(args.input) if args.input else {}
    except json.JSONDecodeError as exc:
        print(f"--input/--daily-input is not JSON: {exc}", file=sys.stderr)
        return 2
    if at_minutes is not None and daily_input is None:
        print("--daily-at needs --daily-input", file=sys.stderr)
        return 2

    data_dir = pathlib.Path(args.data_dir) if args.data_dir else HERE / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # Set before the first `prepare()`; every cycle reads the same directory.
    os.environ["DATA_DIR"] = str(data_dir)

    lock = _acquire_lock(data_dir / "agent.daemon.lock")
    if lock is None:
        print(
            f"another daemon already holds {data_dir / 'agent.daemon.lock'}; "
            "refusing to run two copies",
            file=sys.stderr,
        )
        return 1

    try:
        _local_now(args.timezone)
    except Exception as exc:  # noqa: BLE001 - bad timezone is a startup error
        print(f"bad --timezone {args.timezone!r}: {exc}", file=sys.stderr)
        return 2

    retain_days = int(args.retain_days)
    if retain_days < 0:
        print("--retain-days cannot be negative", file=sys.stderr)
        return 2

    db = _open_cycle_db(data_dir / "agent_cycles.db")
    _prune_cycles(db, retain_days)
    stop = {"flag": False}

    def _stop(_signum, _frame):
        stop["flag"] = True

    import signal

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass

    _log(
        {
            "at": _iso_now(),
            "event": "daemon_started",
            "pid": os.getpid(),
            "interval_seconds": interval,
            "daily_at": args.daily_at or None,
            "data_dir": str(data_dir),
        },
        data_dir,
    )

    cycles = 0
    next_tick = time.monotonic()
    try:
        while not stop["flag"]:
            now = _local_now(args.timezone)
            today = now.date().isoformat()

            # Both runs are independent. A failed monitor must not suppress the
            # daily report, and a failed report must not stop monitoring.
            _run_cycle(db, monitor_input, "monitor", data_dir, interval, args.daily_at)

            if (
                at_minutes is not None
                and now.hour * 60 + now.minute >= at_minutes
                and _state_get(db, "last_daily_date", "") != today
            ):
                _run_cycle(db, daily_input or {}, "daily", data_dir, interval, args.daily_at)
                # Marked after the attempt: a failed report is recorded in the
                # cycle log, and is not retried every minute for the rest of the
                # day. The next day still runs.
                _state_set(db, "last_daily_date", today)
                _prune_cycles(db, retain_days)

            cycles += 1
            if args.cycles and cycles >= args.cycles:
                break
            next_tick += interval
            delay = max(0.0, next_tick - time.monotonic())
            while delay > 0 and not stop["flag"]:
                time.sleep(min(delay, 1.0))
                delay = next_tick - time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        _log(
            {"at": _iso_now(), "event": "daemon_stopped", "cycles": cycles},
            data_dir,
        )
        db.close()
        lock.close()
    return 0


def _tools_digest(files: dict[str, str]) -> str:
    """The same content hash the exporter uses, over `tools/*.py`."""
    import hashlib

    sha = hashlib.sha256()
    for name in sorted(files):
        sha.update(name.encode("utf-8"))
        sha.update(b"\\0")
        sha.update(files[name].encode("utf-8"))
        sha.update(b"\\0")
    return sha.hexdigest()[:16]


def cmd_selfcheck(_args: argparse.Namespace) -> int:
    """Recompute the recorded hashes. Detects an edited runtime or definition."""
    import hashlib

    lock = json.loads((HERE / "agent.lock").read_text(encoding="utf-8"))

    runtime: dict[str, str] = {}
    for path in sorted((HERE / "vendor" / "runtime" / "app").rglob("*.py")):
        runtime[path.relative_to(HERE / "vendor" / "runtime" / "app").as_posix()] = (
            path.read_text(encoding="utf-8")
        )
    sha = hashlib.sha256()
    for name in sorted(runtime):
        sha.update(name.encode("utf-8"))
        sha.update(b"\\0")
        sha.update(runtime[name].encode("utf-8"))
        sha.update(b"\\0")
    actual_runtime = sha.hexdigest()[:16]

    actual_graph = hashlib.sha256((HERE / "agent.yaml").read_bytes()).hexdigest()[:16]

    problems = []
    if actual_runtime != lock["runtime_hash"]:
        problems.append(
            f"runtime changed: expected {lock['runtime_hash']}, found {actual_runtime}"
        )
    if actual_graph != lock["graph_hash"]:
        problems.append(
            f"agent.yaml changed since it was exported (expected "
            f"{lock['graph_hash']}, found {actual_graph}). Re-export to record the "
            "new definition, or keep this edit knowingly."
        )

    # Tools are hashed separately from the definition because they are edited by
    # different people at different times: "agent.yaml changed" would be the wrong
    # message half the time, and a message that names the wrong file is worse than
    # no name at all.
    tools = {}
    for path in sorted((HERE / "tools").glob("*.py")):
        if path.name == "__init__.py":
            continue
        # Keyed by module name, not filename, because that is how the exporter
        # keys it — it hashes the definition's `tools` mapping, which holds
        # module names. Keying one side by `query_order.py` and the other by
        # `query_order` makes every package fail its own selfcheck.
        tools[path.stem] = path.read_text(encoding="utf-8")
    actual_tools = _tools_digest(tools)
    if lock.get("tools_hash") and actual_tools != lock["tools_hash"]:
        changed = sorted(tools)
        problems.append(
            f"agent tools changed since export (expected {lock['tools_hash']}, "
            f"found {actual_tools}). Files: {', '.join(changed)}."
        )

    try:
        from app.graph.compiler import compile_graph

        compile_graph(load_agent())
    except Exception as exc:  # noqa: BLE001
        problems.append(f"agent.yaml does not compile: {type(exc).__name__}: {exc}")

    try:
        prepare()
    except SystemExit as exc:
        problems.append(f"tools did not load (exit {exc.code})")

    for problem in problems:
        print(f"FAIL  {problem}")
    print(f"RESULT={'FAIL' if problems else 'PASS'} ({len(problems)} problems)")
    return 1 if problems else 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Check the package's capabilities: packs, dependencies, tool wiring.

    Distinct from `selfcheck`, which answers "is this package the one that was
    exported?". This one answers "will it actually do what it claims on *this*
    machine?" -- a missing `openpyxl`, a pack that a hand-edited manifest names
    but the runtime does not ship, a tool the graph references but nothing
    provides. Those are deployment problems, not tampering.
    """
    from app.runtime.packs.registry import doctor_report, format_doctor

    prepare()
    report = doctor_report()
    print(format_doctor(report))

    declared = load_manifest().get("packs")
    pack_line = (
        ", ".join(str(name) for name in declared if name)
        if isinstance(declared, list)
        else "(not declared; exported before packs existed)"
    )
    print("manifest packs: " + pack_line)
    print("workspace: " + os.environ.get("AGENT_WORKSPACE_DIR", "(unset)"))

    problems = list(report.get("problems") or [])
    from app.tools.registry import ToolError, get_tool_registry

    registry = get_tool_registry()
    for name in sorted(_referenced_tools()):
        try:
            registry.get(name)
        except ToolError:
            problems.append(f"tool '{name}' is referenced by agent.yaml but not available")
    for problem in problems:
        print(f"PROBLEM: {problem}")
    print("RESULT=" + ("PASS" if not problems else "FAIL"))
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run once")
    run.add_argument("--input", default="", help="JSON object of inputs")
    run.set_defaults(func=cmd_run)
    serve = sub.add_parser(
        "serve", help="JSON lines on stdin, or the HTTP chat API with --http"
    )
    serve.add_argument(
        "--http",
        action="store_true",
        help="serve the HTTP API + built-in chat page instead of stdin JSON lines",
    )
    serve.add_argument(
        "--host", default=os.environ.get("AGENT_HOST", "127.0.0.1")
    )
    serve.add_argument(
        "--port", type=int, default=int(os.environ.get("AGENT_PORT", "8080"))
    )
    serve.add_argument(
        "--token",
        default=os.environ.get("AGENT_HTTP_TOKEN", ""),
        help="bearer token required on /v1/* and /api/info (default: AGENT_HTTP_TOKEN)",
    )
    serve.set_defaults(func=cmd_serve)
    daemon = sub.add_parser(
        "daemon",
        help="run continuously (bounded cycle per interval)",
        description=_DAEMON_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    daemon.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("AGENT_INTERVAL_SECONDS", "60")),
        help="seconds between monitor cycles (default: AGENT_INTERVAL_SECONDS or 60)",
    )
    daemon.add_argument(
        "--input",
        default=os.environ.get("AGENT_INPUT", "{}"),
        help="JSON inputs for each monitor cycle (default: AGENT_INPUT)",
    )
    daemon.add_argument(
        "--daily-at",
        default=os.environ.get("AGENT_DAILY_AT", ""),
        help="HH:MM local time for a daily run; empty disables it (default: AGENT_DAILY_AT)",
    )
    daemon.add_argument(
        "--daily-input",
        default=os.environ.get("AGENT_DAILY_INPUT", ""),
        help="JSON inputs for the daily run (default: AGENT_DAILY_INPUT)",
    )
    daemon.add_argument(
        "--timezone",
        default=os.environ.get("AGENT_TIMEZONE", ""),
        help="IANA zone for --daily-at, e.g. Asia/Shanghai (default: host local)",
    )
    daemon.add_argument(
        "--data-dir",
        default=os.environ.get("AGENT_DATA_DIR", ""),
        help="state, logs and results.db (default: AGENT_DATA_DIR or ./data)",
    )
    daemon.add_argument(
        "--cycles",
        type=int,
        default=int(os.environ.get("AGENT_CYCLES", "0")),
        help="stop after N intervals; 0 means forever (default: AGENT_CYCLES or 0)",
    )
    daemon.add_argument(
        "--retain-days",
        type=int,
        default=int(os.environ.get("AGENT_RETAIN_DAYS", "365")),
        help=(
            "prune the daemon's own cycle log after N days; 0 keeps forever "
            "(default: AGENT_RETAIN_DAYS or 365). results.db is never pruned."
        ),
    )
    daemon.set_defaults(func=cmd_daemon)
    sub.add_parser("selfcheck", help="verify the package").set_defaults(func=cmd_selfcheck)
    sub.add_parser(
        "doctor", help="check capability packs and their dependencies"
    ).set_defaults(func=cmd_doctor)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
'''

__all__ = ["MAIN", "TOOLS_EXAMPLE", "TOOLS_INIT"]
