"""Turn a saved workflow into a package that runs on its own.

What "on its own" has to mean
-----------------------------
An exported agent runs on a machine that has Python and pip and nothing else:
no canvas, no platform database, no SQLite file. That is the whole point of the
feature — the canvas is a mould, and the part that comes out of it is a part.

The one thing it *does* need is the runtime: the compiler, the node types, the
capability layer. Today that is copied into the package (`vendor/runtime/`) with a
recorded hash, and the copy is deliberate rather than an oversight:

* the runtime is not a distributable package yet (task 27a), so `pip install` is
  not an option;
* a copy makes the package runnable **now**, which is worth more than a promise;
* the hash in `agent.lock` is what keeps the copy honest — it is computed from the
  files as written, and `selfcheck` recomputes it, so an edited runtime is
  detected rather than silently different from every other agent.

When the runtime becomes a wheel, this module changes one thing: emit a
requirement instead of copying. The manifest already records both the version and
the hash, so nothing else moves.

What is *never* in the package
------------------------------
Credentials. Node configs can hold an `api_key`, and the export walks them for
credential-shaped keys and replaces the values with `${...}` references to the
`.env.example` names. A package that carries a working key is a package that
leaks one the first time it is shared, and "the export format keeps secrets" is
not a thing anyone should have to remember.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
from dataclasses import dataclass, field
from typing import Any

import yaml

from app.graph.schema import WorkflowGraph
from app.runtime.export_templates import MAIN, TOOLS_EXAMPLE, TOOLS_INIT
from app.runtime.serialize import SECRET_HINTS

#: Where the runtime modules live inside the package.
#:
#: The `app/` level is not decoration: the runtime's own modules import each other
#: as `app.graph...`, `app.nodes...`, so the package has to present the same top
#: level name or every internal import fails. `main.py` puts this directory on
#: `sys.path`.
RUNTIME_DIR = "vendor/runtime/app"

#: Modules copied into the package: the runtime, and only the runtime.
#:
#: Listed explicitly rather than discovered, because discovery is how a platform
#: module gets in by accident one day and the failure shows up as
#: `ModuleNotFoundError: app.db` on a customer's machine. A test asserts every one
#: of these exists, so a rename breaks here rather than at delivery.
RUNTIME_PATHS = (
    # The runtime's model-calling half of the old `app/ai/` package. The
    # authoring half (gen_graph / gen_node / repair / prompts / parsing) is
    # platform-only and deliberately absent — an exported agent is handed a
    # definition and runs it; it never writes one. See `app/ai_buildtime/`.
    "runtime/ai/__init__.py",
    "runtime/ai/gateway.py",
    "runtime/ai/messages.py",
    "runtime/ai/provider.py",
    # ---- capability packs -------------------------------------------------
    # The pack framework and the two packs shipped with this runtime. Packs are
    # part of the runtime rather than of the agent package on purpose: the same
    # implementation the canvas runs ships to the customer, which is what makes
    # `manifest.yml`'s `packs:` list a declaration instead of a promise. The
    # files pack imports openpyxl/python-docx/pypdf lazily, so a deployment
    # that never uses it never imports them -- but the lock still pins them
    # because an agent whose workflow does use the pack cannot run without them.
    "runtime/packs/__init__.py",
    "runtime/packs/manifest.py",
    "runtime/packs/registry.py",
    "runtime/packs/builtin/__init__.py",
    "runtime/packs/builtin/core.py",
    "runtime/packs/builtin/files.py",
    # The policy/audit/workspace seams the packs act through. Listed here
    # because the pack modules import them at module level; omitting one makes
    # the vendored runtime fail to start, not merely lose a feature.
    "runtime/permissions.py",
    "runtime/audit.py",
    "runtime/workspace.py",
    # Conversation state and the standalone HTTP surface. Both are stdlib-only
    # by construction: the layer-boundary test forbids FastAPI/Starlette in any
    # runtime module, which is exactly why the exported agent can serve HTTP
    # with no web-framework dependency at all.
    "runtime/session.py",
    "runtime/http_api.py",
    "graph/__init__.py",
    "graph/compiler.py",
    "graph/schema.py",
    "graph/state.py",
    "graph/subgraphs.py",
    "graph/validate.py",
    "nodes/__init__.py",
    "nodes/agent.py",
    "nodes/base.py",
    "nodes/code.py",
    "nodes/condition.py",
    "nodes/control.py",
    "nodes/end.py",
    "nodes/human.py",
    "nodes/llm.py",
    "nodes/prompt.py",
    "nodes/registry.py",
    "nodes/retriever.py",
    "nodes/start.py",
    "nodes/subgraph.py",
    "nodes/supervisor.py",
    "nodes/tool.py",
    # Business connectors. Listed because `nodes/__init__.py` imports it at
    # module level; omitting it makes every exported agent fail to import the
    # runtime, not merely lack these node types.
    "nodes/integrations.py",
    # Memory ships with the agent rather than staying behind. An exported agent
    # whose memory node cannot import does not degrade to "no memory" — the
    # import is at module level, so the whole graph fails to load. Measured: this
    # is exactly how the omission surfaced, as four broken export tests.
    #
    # `goals.py` and `goals_node.py` are listed for the same reason, and that this
    # bit twice is the argument for `tests/test_export_agent.py`'s manifest test:
    # a hand-maintained list fails silently, so it needs a check rather than care.
    "memory/__init__.py",
    "memory/goals.py",
    "memory/goals_node.py",
    "memory/nodes.py",
    "memory/store.py",
    "runtime/__init__.py",
    "runtime/attempts.py",
    "runtime/budget.py",
    "runtime/checkpoints.py",
    "runtime/egress.py",
    "runtime/events.py",
    # The wind-down forecast. An exported agent has no budget by default (it runs
    # one workflow and stops), but the compiler imports `project` at module level,
    # so omitting this makes the package fail to *start* rather than simply not
    # wind down. Measured: 5 export tests, and this is the fourth time a new
    # runtime module has been forgotten here.
    "runtime/projection.py",
    "runtime/export_templates.py",
    "runtime/expr.py",
    "runtime/manager.py",
    "runtime/queue.py",
    "runtime/runner.py",
    "runtime/runs.py",
    "runtime/sandbox.py",
    "runtime/sandbox_child.py",
    "runtime/serialize.py",
    "runtime/template.py",
    # Trace ids for log correlation (task 25). Listed because `main.py`
    # imports it at module level and the log formatter names the field.
    "runtime/trace.py",
    # The seam `manager.py` uses to load a definition when it rebuilds a run.
    # Listed because the import is at module level: without it an exported agent
    # cannot import its own run manager. The provider itself is null in a
    # standalone deployment, which never rebuilds — but the module has to be
    # there for the import to resolve.
    "runtime/workflows.py",
    "security/__init__.py",    # Standard library only, and `security/__init__.py` re-exports it — so the
    # package cannot import `app.security.secrets` without it. `deps.py` is left
    # out on purpose: it is the FastAPI middleware, which is platform surface.
    "security/auth.py",
    "security/secrets.py",
    "settings.py",
    "tools/__init__.py",
    "tools/builtin.py",
    "tools/loading.py",
    "tools/registry.py",
    # The results-database tools. Added 2026-10-16, and it is the fifth entry
    # this list has needed after the fact — see the note above `memory/`.
    #
    # `tools/__init__.py` imports this module for its registration side effect,
    # so omitting it does not degrade to "the db tools are unavailable"; it makes
    # the vendored `app.tools` fail to import at all, and every export test goes
    # red with `cannot import name 'results_db' from partially initialized
    # module`. Measured, not predicted.
    "tools/results_db.py",
)

#: Config keys whose values are credentials. Sourced from the redaction module so
#: "what counts as a credential" has one definition rather than two that drift.
CREDENTIAL_KEYS = SECRET_HINTS


@dataclass
class ExportFile:
    path: str
    content: str


@dataclass
class ExportResult:
    """What was written, and the facts needed to verify it later."""

    root: pathlib.Path
    files: list[str] = field(default_factory=list)
    #: sha256 of the copied runtime, truncated. Recorded in `agent.lock` and
    #: recomputed by `selfcheck`.
    runtime_hash: str = ""
    #: sha256 of the agent definition (the graph after credential stripping).
    graph_hash: str = ""
    #: Credential keys whose values were replaced with `${...}` placeholders.
    redacted_keys: list[str] = field(default_factory=list)


class ExportError(RuntimeError):
    """Raised when a workflow cannot be exported."""


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


def _strip_credentials(graph: WorkflowGraph) -> tuple[WorkflowGraph, list[str]]:
    """Replace credential values with env placeholders, everywhere in the graph.

    Walks the raw dict form rather than the model so a credential hiding in a
    parameter we do not know about is still found — the point is to catch what is
    there, not what the schema lists.
    """
    payload = graph.model_dump(mode="json")
    found: list[str] = []

    def walk(value: Any, node_id: str) -> Any:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and key.lower() in CREDENTIAL_KEYS:
                    if item:  # an empty value is not a secret
                        env_name = f"{node_id.upper().replace('-', '_')}_{key.upper()}"
                        found.append(f"{node_id}.{key}")
                        out[key] = "${" + env_name + "}"
                    else:
                        out[key] = item
                else:
                    out[key] = walk(item, node_id)
            return out
        if isinstance(value, list):
            return [walk(item, node_id) for item in value]
        return value

    # Walked as a plain dict and re-validated, rather than `model_copy(update=...)`
    # with hand-built node dicts: that version serialised dicts where `NodeSpec`
    # was declared and emitted a Pydantic warning on every export.
    payload["nodes"] = [
        {**node, "config": walk(node.get("config") or {}, node.get("id", "node"))}
        for node in payload["nodes"]
    ]
    return WorkflowGraph.model_validate(payload), sorted(set(found))


#: Node config keys whose value is *logic* rather than settings, and the folder
#: each one is written to. This is `delivery.md` §5 step 3, "拆文件".
#:
#: The point is what the customer's editor gets: a `.md` with syntax highlighting
#: and a `.py` the IDE can check, instead of a string escaped inside JSON. What
#: does **not** move is equally deliberate — a condition node's `expression`
#: stays in the definition, because a short expression split into its own file is
#: harder to read, not easier.
SPLIT_LOGIC = {
    "template": ("prompts", ".md"),
    # The `llm` node calls its field `prompt`, not `template` — a different name
    # for the same idea. Missing this meant an `llm` node's prompt stayed inline
    # while a `prompt` node's was externalised, which is exactly the inconsistency
    # `delivery.md` §5 step 3 exists to remove. Found by exporting a graph that
    # contains an `llm` node; the export tests only ever used `prompt` nodes.
    "prompt": ("prompts", ".md"),
    "code": ("code", ".py"),
}

#: Key under which a moved value is referenced.
#:
#: `!ref` is a YAML tag, resolved back to the file's text by `main.py`. It is the
#: spelling `delivery.md` §6 specifies, and PyYAML supports application tags via
#: `add_constructor` (see `_yaml_definition`).
REF_KEY = "!ref"


class _RefLoader(yaml.SafeLoader):
    """A safe loader that understands `!ref`.

    Subclassed rather than patched onto `SafeLoader` so registering the tag
    cannot leak into another loader — `yaml.add_constructor` without a loader
    argument mutates the global `Loader`.
    """


def _ref_constructor(loader: yaml.Loader, node: yaml.Node) -> Any:
    """A `!ref path/to/file` becomes the file's text.

    Bound to the package root by the caller, which is why this is looked up
    through the loader's own `_agent_root` rather than a module global: two
    packages loaded in one process must not share a root.
    """
    path = loader.construct_scalar(node)
    root: pathlib.Path = getattr(loader, "_agent_root", pathlib.Path("."))
    target = root / path
    if not target.exists():
        raise ExportError(f"agent.yaml references '{path}', which is missing.")
    return target.read_text(encoding="utf-8")


_RefLoader.add_constructor(REF_KEY, _ref_constructor)


class _Ref(str):
    """A string that dumps as ``!ref path/to/file``.

    A `str` subclass rather than a dict, because that is what the tag means: the
    value *is* the path, and `!ref prompts/x.md` reads as one thing where
    `{!ref: prompts/x.md}` reads as a mapping with a strange key.

    Subclassing `str` also keeps it usable as a string everywhere else, which
    matters because `_split_logic` runs before serialisation and the payload may
    still be validated against the graph schema in between.
    """


def _ref_representer(dumper: yaml.Dumper, data: _Ref) -> yaml.Node:
    return dumper.represent_scalar(REF_KEY, str(data))


class _NoAliasDumper(yaml.SafeDumper):
    """A `SafeDumper` that never emits `&anchor` / `*alias`.

    PyYAML emits anchors for any object it sees twice, and the graph schema
    shares dicts internally, so the defaults produce a file dotted with
    references the customer has to decode. Disabling them costs a little size and
    buys a file that reads top to bottom.
    """

    def ignore_aliases(self, data: Any) -> bool:
        return True


_NoAliasDumper.add_representer(_Ref, _ref_representer)


def _yaml_definition(payload: dict[str, Any], root: pathlib.Path) -> str:
    """The definition as `agent.yaml`, with ``!ref`` for externalised logic.

    `delivery.md` §6: "no longer escaped JSON, but human-writable YAML". The
    reason is the customer: they edit this file, and a prompt template reads far
    better in YAML's block scalars than as a JSON string full of `\\n`.

    **Aliases are disabled.** PyYAML turns a shared object into `&anchor`/`*alias`
    by default, which is smaller and completely unreadable — and the graph schema
    shares dicts internally, so the defaults would emit them. `ignore_aliases`
    makes every node independent.
    """
    text = yaml.dump(
        payload,
        Dumper=_NoAliasDumper,
        allow_unicode=True,
        sort_keys=False,      # preserve node order, which is the author's
        default_flow_style=False,
        width=100,
    )
    return text


def _split_logic(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Move prompt and code text out of the definition into their own files.

    Returns the definition with each moved field replaced by ``{"$ref": path}``,
    plus the files to write. Pure — nothing is written here.

    A node whose field is empty is left alone: an empty `prompts/x.md` is a file
    that says nothing, and creating one per unused field would litter the package.
    """
    files: dict[str, str] = {}
    for node in payload.get("nodes") or []:
        config = node.get("config")
        if not isinstance(config, dict):
            continue
        node_id = node.get("id") or "node"
        for key, (folder, suffix) in SPLIT_LOGIC.items():
            value = config.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            rel = f"{folder}/{node_id}{suffix}"
            # A node id is only constrained by the graph schema, so it can hold a
            # character a path cannot. Rejecting is better than silently writing
            # to a path that resolves somewhere else.
            if any(bad in node_id for bad in ("/", "\\", "..")):
                raise ExportError(
                    f"Node '{node_id}' cannot have its {key} split into a file: "
                    "the id contains a path separator."
                )
            # Written verbatim. Adding a trailing newline would look tidier in an
            # editor and would silently change the template — the split has to be
            # lossless or the exported agent answers differently than the canvas
            # did. Measured: adding one broke both run tests with `'ORDER A-1\n'`.
            files[rel] = value
            # The plain path. `_tag_refs` wraps it for YAML at serialisation
            # time — see the note in `build_export` for why not here.
            config[key] = rel
    return payload, files


def _tag_refs(payload: dict[str, Any], written: set[str]) -> dict[str, Any]:
    """Wrap every value that names a split file in :class:`_Ref`.

    Done on the *serialised* side of validation, because Pydantic would
    otherwise strip the subclass and the YAML would lose the `!ref` tag.
    Recognition is by the value matching a path this export wrote, not by key
    name alone: a condition node's `expression` may legitimately contain a
    string that looks like a path, and tagging it would make `main.py` read a
    file that has nothing to do with it.

    ``written`` is passed rather than read from a module global so two exports
    in one process cannot see each other's paths.
    """
    for node in payload.get("nodes") or []:
        config = node.get("config")
        if not isinstance(config, dict):
            continue
        for key in SPLIT_LOGIC:
            value = config.get(key)
            if isinstance(value, str) and value in written:
                config[key] = _Ref(value)
    return payload


def _digest(files: dict[str, str]) -> str:
    """A stable hash over a set of files.

    Content-based, so it survives reformatting a git checkout, and order-stable so
    two exports of the same thing agree.
    """
    sha = hashlib.sha256()
    for name in sorted(files):
        sha.update(name.encode("utf-8"))
        sha.update(b"\0")
        sha.update(files[name].encode("utf-8"))
        sha.update(b"\0")
    return sha.hexdigest()[:16]


def runtime_files(app_root: pathlib.Path) -> dict[str, str]:
    """Read the runtime modules that go into a package, keyed by relative path."""
    out: dict[str, str] = {}
    for rel in RUNTIME_PATHS:
        path = app_root / rel
        if not path.exists():
            raise ExportError(
                f"Runtime module '{rel}' is missing from {app_root}. The export "
                "cannot be verified against the runtime that runs it."
            )
        out[rel] = path.read_text(encoding="utf-8")
    return out


#: Where a locked, hashed requirements file is looked for. Produced at release
#: time by `scripts/build_vendor.py`; absent in a dev checkout, which is why the
#: exporter has a second, unsealed form below rather than failing.
REQUIREMENTS_LOCK_NAME = "_vendor_requirements.txt"

#: The lock file, resolved next to `app/` (i.e. in `backend/`). Module-level so
#: `build_export` and `_requirements_txt` cannot disagree about where it is.
REQUIREMENTS_SEALED_FILE = pathlib.Path(__file__).resolve().parents[2] / REQUIREMENTS_LOCK_NAME


def _requirements_txt(runtime_version: str, app_root: pathlib.Path) -> str:
    """The agent's dependencies — sealed when a lock file is available.

    Two forms, and the difference is the whole of task 27c:

    * **Sealed** (`_vendor_requirements.txt` exists beside the app): every
      dependency pinned to an exact version with a `--hash=`. Installed with
      `--no-index --find-links vendor/ --require-hashes`, pip refuses anything
      whose bytes differ — so "every agent runs the same runtime" is enforced by
      the installer rather than promised by a docstring.
    * **Unsealed** (no lock file, e.g. a fresh checkout): the same direct
      dependencies as ranges. The package still runs; it just is not reproducible,
      and `agent.lock` records that by carrying no `requirements_sealed` flag.

    The runtime itself is **not** listed either way: it is vendored under
    `vendor/runtime/`. When it becomes a wheel (the rest of 27a/27b) this file
    gains one more `agent-scaffold-runtime==<version> --hash=...` line.
    """
    header = (
        f"# Generated by the agent exporter. Runtime version: {runtime_version}\n"
        "#\n"
        "# The runtime is vendored under `vendor/runtime/`, not installed from a\n"
        "# package, so it is deliberately absent here. See README.md.\n"
    )

    lock = REQUIREMENTS_SEALED_FILE
    if lock.exists():
        # Verbatim from the lock: re-deriving it here would be a second source of
        # truth for the hashes, and the one that drifts is the one that matters.
        return header + "#\n# Sealed: every pin carries a hash. Install with\n" + (
            "#   pip install --no-index --find-links vendor/ --require-hashes -r "
            "requirements.txt\n"
        ) + lock.read_text(encoding="utf-8").strip() + "\n"

    return header + (
        "#\n# NOT SEALED — no lock file was found at build time. Versions are ranges\n"
        "# and there are no hashes, so this package is not byte-reproducible.\n"
        "langgraph==1.2.11\n"
        "langchain-core>=1.0,<2.0\n"
        "langchain>=1.0,<2.0\n"
        "langchain-openai>=1.0,<2.0\n"
        "pydantic>=2.0,<3.0\n"
        "pydantic-settings>=2.0,<3.0\n"
        # `main.py` loads `agent.yaml`, and `!ref` needs a YAML parser.
        "pyyaml>=6.0,<7.0\n"
        # Named only by lazy imports inside `runtime/checkpoints.py`, so nothing
        # at module level pulls them in — and nothing else depends on them.
        # Without these the package installs cleanly and fails at the first run
        # under `CHECKPOINT_STORE=sqlite`.
        "langgraph-checkpoint-sqlite>=3.0,<4.0\n"
        "aiosqlite>=0.20,<1.0\n"
        # The files capability pack's readers/writers, imported lazily by
        # `runtime/packs/builtin/files.py`. In the sealed form the lock already
        # pins them; this list only matters on a dev checkout, where there is no
        # lock and the package is installed from the index.
        "openpyxl>=3.1,<4.0\n"
        "python-docx>=1.1,<2.0\n"
        "pypdf>=5,<7.0\n"
    )


#: Input names a chat session may use for the user's message, in preference
#: order. The exporter records the chosen one in `manifest.yml`; the standalone
#: session layer then fills that input, so a workflow author never has to know
#: an HTTP layer exists.
_MESSAGE_INPUT_KEYS = ("message", "text", "query", "input", "question")


def _start_input_keys(graph: WorkflowGraph) -> list[str]:
    """The Start node's declared inputs, in declaration order."""
    for node in graph.nodes:
        if node.type != "start":
            continue
        config = node.config or {}
        schema = config.get("input_schema")
        if isinstance(schema, dict):
            return [str(key) for key in schema]
        defaults = config.get("inputs")
        if isinstance(defaults, dict):
            return [str(key) for key in defaults]
        return []
    return []


def _session_message_key(graph: WorkflowGraph) -> str:
    """Which Start input a chat request should fill in.

    The workflow says it: an input called `message` (or `text` / `query` / …) is
    the obvious one; failing all of those, the first declared input. The result
    is written into `manifest.yml` rather than re-derived at request time, so a
    deployer can see it and change it -- a session layer that guessed silently
    would fill the wrong field and look like a prompt bug.
    """
    keys = _start_input_keys(graph)
    lowered = {key.lower(): key for key in keys}
    for candidate in _MESSAGE_INPUT_KEYS:
        if candidate in lowered:
            return lowered[candidate]
    return keys[0] if keys else "message"


def _packs_for_graph(graph: WorkflowGraph) -> list[str]:
    """Capability packs the definition's tool references require, by name.

    Read from the graph rather than from the manifest: a hand-edited
    `agent.yaml` must not end up with a pack list that disagrees with its nodes.
    A name no pack provides (a typo, or an agent-bundled tool) maps to no pack;
    compile/run reports those separately.
    """
    from app.runtime.packs.registry import get_pack_registry

    referenced: set[str] = set()
    for node in graph.nodes:
        config = node.config or {}
        if node.type == "tool" and config.get("tool"):
            referenced.add(str(config["tool"]))
        elif node.type == "agent" and isinstance(config.get("tools"), list):
            referenced.update(str(name) for name in config["tools"] if name)
    return get_pack_registry().packs_for_tools(sorted(referenced))


def _manifest_yml(
        graph: WorkflowGraph, redacted: list[str], runtime_version: str
) -> str:
        """The capability declaration — `delivery.md` §1 item 2.

        What it carries, and why each part is here rather than re-derived:

        * **which tools** this agent uses — the names only. The implementations
            are in `tools/`, and the agent's own tools are registered by importing
            them; this list exists so a deployer can see what it reaches for
            without reading Python.
        * **which capability packs** provide those tools. The exported runtime
            loads exactly these packs, so this is the line that keeps a deployed
            agent to the capabilities its definition declared.
        * **the session entry keys** — `message_key` / `history_key`. The chat /
            HTTP layer fills these Start inputs; both are ordinary inputs, so a
            normal run can pass them too.
        * **which sandbox tier** — `local` by default, matching the conservative
            default `delivery.md` §5 asks for. `container` is the production tier and
            has to be chosen explicitly.
        * **which credential names** — names, never values. Same list the
            `.env.example` carries, from the same redaction pass.

        Written as YAML by hand rather than with `pyyaml.dump`: the file is a few
        keys and short lists, and a dump would wrap them differently on every
        library upgrade for no gain. This is the one place the exporter emits YAML,
        so a dependency on a dumper for it would be the tail wagging the dog.
        """
        tool_names = sorted((graph.tools or {}).keys())
        credential_names = sorted(
                key.upper().replace(".", "_").replace("-", "_") for key in redacted
        )
        pack_names = _packs_for_graph(graph)
        message_key = _session_message_key(graph)

        lines = [
                "# Generated by the agent exporter. Runtime version: " + runtime_version,
                "#",
                "# What this agent needs to run. Names only — no values, no secrets.",
                "",
                "# The packaged agent's identity. The HTTP/chat layer shows it, and",
                "# nothing else in the runtime depends on it.",
                "name: " + json.dumps(graph.name or "agent", ensure_ascii=False),
                "",
                "tools:",
        ]
        lines += [f"  - {name}" for name in tool_names] if tool_names else ["  []"]
        lines += [
                "",
                "# Capability packs whose tools this definition references. The",
                "# deployed runtime loads exactly these packs, plus the tools bundled",
                "# above, so an agent cannot reach a capability it did not declare.",
                "packs:",
        ]
        lines += [f"  - {name}" for name in pack_names] if pack_names else ["  []"]
        lines += [
                "",
                "# Chat entry. `message_key` is the Start input the session endpoint",
                "# fills with the user's message; `history_key` receives the transcript.",
                "# Both can be overridden at deploy time with AGENT_MESSAGE_KEY /",
                "# AGENT_HISTORY_KEY.",
                "session:",
                f"  message_key: {message_key}",
                "  history_key: history",
                "",
                "# The isolation tier. `container` is the production choice and has to be",
                "# asked for; `local` runs the code in a subprocess.",
                "sandbox:",
                "  runner: local",
                "",
                "# Credential NAMES this agent expects. Values are supplied at deploy time",
                "# (see config/.env.example) and are never part of the package.",
                "credentials:",
        ]
        lines += [f"  - {name}" for name in credential_names] if credential_names else ["  []"]
        return "\n".join(lines) + "\n"


def _env_example(redacted: list[str]) -> str:
    header = """# Copy to `config/secrets.env` and fill in.
#
# These values are NOT in the package: the exporter replaced every credential it
# found in the workflow definition with a reference to a name below. Fill them in
# here (or supply them as environment variables) and the agent picks them up.
"""
    lines = [header]
    if redacted:
        for key in redacted:
            env_name = key.upper().replace(".", "_").replace("-", "_")
            lines.append(f"{env_name}=")
    else:
        lines.append("# No credentials were found in the workflow definition.")
    lines.append("")
    lines.append("# The model endpoint this agent talks to.")
    lines.append("LLM_BASE_URL=")
    lines.append("LLM_MODEL=")
    lines.append("LLM_API_KEY=")
    return "\n".join(lines) + "\n"


def _dockerfile() -> str:
    return """# Two stages so the runtime layer is cached separately from the agent's own
# files: editing a prompt must not rebuild the dependency layer.
FROM python:3.13-slim AS deps
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.13-slim
WORKDIR /app
COPY --from=deps /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY . .
# The runtime is vendored, so no install step is needed for it.
ENV PYTHONUNBUFFERED=1
# State, logs and results.db live here; mount it so a rebuilt container keeps
# the daily-report marker and the telemetry the workflow wrote.
VOLUME ["/app/data"]
ENTRYPOINT ["python", "main.py"]
# Verify a fresh image by default. The long-running form is:
#   docker run -d --restart unless-stopped --env-file config/secrets.env \\
#       -v "$PWD/data:/app/data" <image> daemon --interval 60 ...
CMD ["selfcheck"]
"""


def _readme(workflow: WorkflowGraph, runtime_version: str, redacted: list[str]) -> str:
    return f"""# {workflow.name}

An agent exported from the canvas. It runs on its own: the canvas is not
involved, and neither is the platform database.

## Run it

```bash
pip install -r requirements.txt
python main.py selfcheck            # verify the package is intact
python main.py run --input '{{"query": "hello"}}'
```

Or with Docker:

```bash
docker build -t {workflow.id} .
docker run --rm {workflow.id} selfcheck
```

## Run it continuously

An exported package has no platform scheduler. `daemon` is that scheduler: it
runs one bounded cycle per interval, writes its own audit log, and keeps a daily
reporting marker on disk.

```bash
python main.py daemon \\
  --interval 60 \\
  --input '{{"line_id":"IM-07","mode":"monitor"}}' \\
  --daily-at 08:00 \\
  --timezone Asia/Shanghai \\
  --daily-input '{{"line_id":"IM-07","mode":"daily_report"}}'
```

- A failing cycle is recorded and the next tick still runs. The process exits
  non-zero only for a startup error (bad arguments, another instance already
  holds the lock), which is what tells a supervisor to restart it.
- The daily run happens at most once per local date. It is marked after the
  attempt, so a failed report is in the log and is not retried every minute; if
  the machine is off for the whole day, there is no report for that day.
- `data/agent_cycles.db` holds one row per cycle (status, duration, error,
  output). `data/health.json` is the cheap check for a dashboard or watchdog.
  `data/agent.log` is JSON lines, rotated at 10 MB. `data/results.db` is written
  by this agent's `db_write` nodes and is **not** pruned by the daemon.
- The cycle log keeps 365 days by default (`--retain-days 0` keeps forever).
  A year at one cycle per minute is 525,600 rows; pruned at start and after each
  daily run.
- A lock file under `data/` refuses a second copy. Two daemons would double every
  setpoint write and every email.

The daemon handles a failing *cycle*; a supervisor handles a dead *process*:

```ini
# /etc/systemd/system/im07-agent.service
[Unit]
Description=IM-07 line agent
After=network-online.target

[Service]
WorkingDirectory=/opt/im07-agent
EnvironmentFile=/opt/im07-agent/config/secrets.env
ExecStart=/opt/im07-agent/.venv/bin/python main.py daemon --interval 60 --input '{{"line_id":"IM-07","mode":"monitor"}}' --daily-at 08:00 --timezone Asia/Shanghai --daily-input '{{"line_id":"IM-07","mode":"daily_report"}}'
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

On Windows, run the same command as a scheduled task at logon with "restart on
failure"; Docker users add `--restart unless-stopped` and mount `data/`. In all
three cases the rule is the same: **the supervisor restarts the process, the
daemon survives a bad cycle.**

## Configure it

| What | Where |
|---|---|
| Endpoint, model, credentials | `config/secrets.env` (copy `config/.env.example`) |
| Which tools this agent may use | environment: `TOOLS_ALLOWLIST`, `TOOLS_DENYLIST` |
| Code sandbox | `SANDBOX_RUNNER=local\\|container` |
| Spending limits | `MAX_RUN_TOKENS`, `MAX_RUN_COST`, `MAX_RUN_SECONDS`, `MAX_RUN_STEPS` |

Credentials are **not** in this package. The exporter replaced every one it found
in the workflow with a reference to a name in `config/.env.example`
({len(redacted)} found). Fill those in, or the agent will start and fail on the
first model call with a clear message.

## Change it

| File | Holds |
|---|---|
| `agent.yaml` | the workflow: nodes, edges, prompts, code, expressions |
| `tools/` | this agent's own tools — add a `.py` that calls `register_tool()` |
| `config/` | deployment configuration |

After editing `agent.yaml`, run `python main.py selfcheck` to confirm it still
compiles. The check also compares the recorded hashes, so an accidental edit to
`vendor/runtime/` shows up too.

## What is in the package

```
agent.yaml         the workflow definition
agent.lock         hashes of the runtime and the definition, for selfcheck
main.py            entry point: run / daemon / serve / selfcheck
tools/             this agent's own tools (empty by default)
config/            .env.example and, in production, secrets/
vendor/runtime/    the runtime, so the agent needs nothing from the platform
requirements.txt   third-party dependencies
Dockerfile         two-stage build
tests/             a smoke test you can run after editing
```

**Do not edit `vendor/runtime/`.** It is the same runtime every agent built from
this canvas got, and `selfcheck` will report a hash mismatch. Change it by
re-exporting from the canvas, which keeps every agent on one implementation —
that is what makes "all agents share a foundation" a fact rather than a promise.
"""


def _smoke_test() -> str:
    return '''"""Smoke test: the agent compiles and reports a result.

    pytest test_agent.py

Run this after editing `agent.yaml` or the tools. It is deliberately shallow —
the point is to catch a broken definition before deploying, not to re-test the
runtime.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent


def test_selfcheck_passes():
    result = subprocess.run(
        [sys.executable, str(HERE / "main.py"), "selfcheck"],
        capture_output=True,
        text=True,
        cwd=HERE,
    )
    assert "RESULT=PASS" in result.stdout, result.stdout + result.stderr


def test_the_definition_is_yaml():
    # `!ref` is left unresolved here on purpose: this asserts the file *parses*
    # and has nodes, without needing the referenced files to be present.
    def _ignore(loader, node):
        return loader.construct_scalar(node)

    loader_cls = type("_L", (yaml.SafeLoader,), {})
    loader_cls.add_constructor("!ref", _ignore)
    payload = yaml.load((HERE / "agent.yaml").read_text(encoding="utf-8"), Loader=loader_cls)
    assert payload["nodes"], "the exported workflow has no nodes"
'''


# ---------------------------------------------------------------------------
# The exporter
# ---------------------------------------------------------------------------


def build_export(
    graph: WorkflowGraph,
    app_root: pathlib.Path,
    *,
    runtime_version: str = "0.1.0",
) -> dict[str, str]:
    """All the files of an exported agent, keyed by path relative to the package.

    Pure: nothing is written. That makes it testable without a temporary
    directory, and it means the API can decide where the files go.
    """
    cleaned, redacted = _strip_credentials(graph)
    runtime = runtime_files(app_root)

    # Split *after* credential stripping and *before* serialising, so the
    # redaction applies to the text that lands in `prompts/` too — a key inside a
    # prompt template is exactly the case that walk is for.
    split_payload, logic_files = _split_logic(cleaned.model_dump(mode="json"))
    # Tagged *after* validation, not inside `_split_logic`.
    #
    # `_Ref` is a `str` subclass and Pydantic normalises subclasses on
    # validation, so a ref put in before `model_validate` comes out as a plain
    # string and the YAML loses its `!ref` tag — measured: `template:
    # prompts/a.md` with no tag, and the exported agent then returned that path
    # as the node's output. Validation runs on the paths, then the paths are
    # re-tagged for serialisation.
    cleaned = WorkflowGraph.model_validate(split_payload)

    # Serialised once and hashed from those same bytes: a lock file whose hash is
    # over a re-serialisation would disagree with its own file the first time a
    # json option changed.
    agent_yaml = _yaml_definition(
        _tag_refs(cleaned.model_dump(mode="json"), set(logic_files)), app_root
    )

    files: dict[str, str] = {
        "agent.yaml": agent_yaml,
        "manifest.yml": _manifest_yml(cleaned, redacted, runtime_version),
        "main.py": MAIN,
        "requirements.txt": _requirements_txt(runtime_version, app_root),
        "config/.env.example": _env_example(redacted),
        "Dockerfile": _dockerfile(),
        "README.md": _readme(graph, runtime_version, redacted),
        "tests/test_agent.py": _smoke_test(),
        "tools/__init__.py": TOOLS_INIT,
        # A real, importable module rather than prose: copying it to
        # `lookup_order.py` and adding one import line is a working tool.
        "tools/lookup_order.py.example": TOOLS_EXAMPLE,
        "vendor/runtime/README.md": (
            "# Vendored runtime\n\n"
            "A copy of the runtime this agent was built against. It is copied, not\n"
            "installed, because the runtime is not published as a package yet.\n\n"
            "**Do not edit these files.** `python main.py selfcheck` compares them\n"
            "against the hash in `agent.lock`, and the whole point is that every\n"
            "agent from this canvas runs the same runtime.\n"
        ),
        "agent.lock": json.dumps(
            {
                "runtime_version": runtime_version,
                "runtime_hash": _digest(runtime),
                "graph_hash": hashlib.sha256(agent_yaml.encode("utf-8")).hexdigest()[:16],
                # Hashed separately from the graph so `selfcheck` can name which
                # of the two changed. They are edited by different people at
                # different times, so "agent.yaml changed" would be the wrong
                # message half of the time.
                "tools_hash": _digest(graph.tools or {}),
                "redacted_credentials": redacted,
                # Whether `requirements.txt` carries hashes. On the lock rather
                # than inferred, so a CI gate can assert the delivered package
                # was sealed without re-reading the file and re-deriving hashes.
                "requirements_sealed": REQUIREMENTS_SEALED_FILE.exists(),
                "format": 1,
            },
            indent=2,
        )
        + "\n",
    }

    for rel, content in runtime.items():
        files[f"{RUNTIME_DIR}/{rel}"] = content

    # The extracted prompts and code, at the paths the definition now references.
    files.update(logic_files)

    # The workflow's own tool modules, written as real files. For these the
    # existing `tools/__init__.py` (which lists nothing by default) gains one
    # import per tool, because importing is what registers.
    if graph.tools:
        imports = "\n".join(
            f"from . import {name}  # noqa: F401  - importing registers it"
            for name in sorted(graph.tools)
        )
        files["tools/__init__.py"] = (
            files["tools/__init__.py"].rstrip() + "\n\n" + imports + "\n"
        )
        for name, source in sorted(graph.tools.items()):
            files[f"tools/{name}.py"] = source

    return files


def write_export(files: dict[str, str], target: pathlib.Path) -> ExportResult:
    """Write the package, replacing any previous export at that path.

    **Newlines are pinned to `\\n`**, and that is load-bearing rather than
    cosmetic. `write_text` in text mode translates to the platform's line ending,
    so on Windows the file on disk holds `\\r\\n` while the hash in `agent.lock`
    was computed over `\\n` — and the package then fails its own `selfcheck` on a
    fresh export. Measured: both hashes mismatched until this was pinned.
    """
    if target.exists():
        shutil.rmtree(target)
    for rel, content in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    lock = json.loads(files["agent.lock"])
    return ExportResult(
        root=target,
        files=sorted(files),
        runtime_hash=lock["runtime_hash"],
        graph_hash=lock["graph_hash"],
        redacted_keys=list(lock.get("redacted_credentials") or []),
    )


__all__ = [
    "CREDENTIAL_KEYS",
    "ExportError",
    "ExportResult",
    "REQUIREMENTS_LOCK_NAME",
    "RUNTIME_DIR",
    "RUNTIME_PATHS",
    "build_export",
    "runtime_files",
    "write_export",
]
