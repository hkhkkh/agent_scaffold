"""Code-execution capability — the seam between a Code node and where it runs.

Why this module exists
----------------------

Before it, ``nodes/code.py`` did::

    from app.runtime.sandbox import run_python
    result = await run_python(code, ..., timeout=ctx.settings.node_timeout_seconds)

That single import is a hard architectural commitment. It says *every* agent
built on this scaffold runs untrusted snippets the same way, on the same host,
under the same resource policy as the API process — forever. Swapping in a
container, a remote executor, or a per-tenant isolation profile would mean
editing the node, and every other node that later gained the ability to run
code.

This module replaces that import with a question instead of an answer. The node
asks for *a runner* and the deployment decides which one it gets::

    runner = get_sandbox_runner()
    result = await runner.run(code, ..., timeout=...)

The shape is copied from :mod:`app.runtime.egress` on purpose. That module
already proved the pattern in this codebase — ABC, a default implementation, a
process-wide injection point, and a builder that reads configuration — and two
different patterns for the same idea is how a codebase stops having a pattern.

Why the runner, and not ``ctx``
-------------------------------

Injecting through :class:`~app.nodes.base.NodeContext` was the alternative. It
was rejected because the choice of sandbox is a **deployment** decision, not a
per-node one: an operator sets ``SANDBOX_RUNNER=container`` once and every agent
on that host is isolated. Hanging it off ``ctx`` would also mean every test that
constructs a context has to supply a runner, and the one test that forgets would
silently run snippets for real.

Runners are stateless — ``run`` holds no cross-call state — so a single
process-wide instance is safe under the concurrent runs Phase 4 will add.

What the interface deliberately does not expose
-----------------------------------------------

``memory_limit``, ``network``, and the Docker flags are *not* parameters. They
are resource policy, which belongs to the implementation: a job object takes
bytes, a container takes ``--memory`` plus ``--memory-swap`` plus ``--pids-limit``,
and flattening those into one interface would either leak one backend's model
into the other or reduce both to the weakest common denominator.

What it *does* expose is ``timeout``, because that is the caller's policy
(``node_timeout_seconds`` is a graph-authoring concern), and the four data
arguments, because those are the Code node's documented contract.

Failing closed
--------------

If the configured runner is unusable — daemon down, CLI missing, image absent —
:meth:`ContainerSandboxRunner.run` returns an error result. It **never** falls
back to executing in the API process. That fallback would be silent, and it
would turn "the isolation broke" into "the isolation was quietly removed",
which is the worse of the two failures by a wide margin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from typing import Any

from app.runtime.sandbox import (
    DEFAULT_MEMORY_LIMIT,
    CodeResult,
    check_code,
    run_python,
)
from app.settings import Settings, get_settings

#: Image used by :class:`ContainerSandboxRunner` when none is configured. Built
#: from ``docker/sandbox.Dockerfile``; see the note on that file for what it
#: contains and why the image is not built automatically.
DEFAULT_SANDBOX_IMAGE = "agent-scaffold-sandbox:latest"

#: Bootstrap the container runs.
#:
#: The explicit ``sys.path`` insert is not ceremony. ``-I`` implies ``-P`` on
#: Python 3.11+, so neither the working directory nor ``PYTHONPATH`` contributes
#: to ``sys.path`` — without this line the container starts, fails to import
#: ``app.runtime.sandbox_child``, and every snippet fails with a confusing
#: ``ModuleNotFoundError`` that looks like a broken image. The *local* child
#: process needed exactly the same fix (``sandbox._CHILD_BOOTSTRAP``), which is
#: why this is a template over the working directory rather than a literal:
#: an operator who points ``workdir`` elsewhere must not silently break it.
CONTAINER_BOOTSTRAP = (
    "import sys; sys.path.insert(0, {root!r}); "
    "from app.runtime.sandbox_child import main; raise SystemExit(main())"
)


class SandboxConfigError(ValueError):
    """Raised when the configured runner is not a known kind.

    Deliberately an error rather than a fallback. ``SANDBOX_RUNNER=containers``
    (a typo) must not resolve to "run it locally" — the operator asked for
    isolation and would never learn they did not get it.
    """


class SandboxRunner:
    """Runs a Code node's snippet somewhere and reports what happened.

    Implementations must:

    * return a :class:`CodeResult` for every outcome — including refusal — rather
      than raising, so the node can decide whether a failure ends the run;
    * treat ``timeout`` as a wall-clock budget they actually enforce;
    * never let the snippet's side effects escape into this process, which is the
      whole reason a runner other than the in-process one exists.
    """

    async def run(
        self,
        code: str,
        *,
        inputs: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        vars_: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> CodeResult:
        raise NotImplementedError


@dataclass
class LocalSandboxRunner(SandboxRunner):
    """Default runner: a resource-limited child process on this host.

    This is the behaviour the scaffold already had; naming it makes the
    alternative describable. It isolates *resources* — the child can be killed
    and has a hard memory ceiling — but not the filesystem or the network, since
    the child runs as the backend's user. See :mod:`app.runtime.sandbox`.

    ``mode`` selects between the child process (``"subprocess"``, default) and
    executing in this process (``"inprocess"``). The in-process mode is a test
    and development affordance: it cannot stop a CPU-bound loop and is not a
    boundary. Leaving ``mode`` as ``None`` reads ``SANDBOX_MODE`` from the
    environment, preserving the previous behaviour exactly.
    """

    mode: str | None = None
    memory_limit: int = DEFAULT_MEMORY_LIMIT

    async def run(
        self,
        code: str,
        *,
        inputs: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        vars_: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> CodeResult:
        return await run_python(
            code,
            inputs=inputs,
            state=state,
            vars_=vars_,
            params=params,
            timeout=timeout,
            mode=self.mode,
            memory_limit=self.memory_limit,
        )


@dataclass
class ContainerSandboxRunner(SandboxRunner):
    """Isolation profile for anything untrusted or multi-tenant.

    A container is the only cheap way to get the two things the local runner
    cannot give: a private filesystem and no network. The flags below are not
    decoration — each one closes a specific hole the local runner documents as
    open:

    ``--network none``
        No network namespace at all. The local runner's snippet can reach
        anything the backend can, and the egress policy only covers requests made
        through the ``http_request`` tool, not ``socket`` from a snippet.
    ``--read-only`` + ``--tmpfs``
        The image is immutable and the only writable path is a small ``/tmp``.
        The local runner's snippet can rewrite the backend's files.
    ``--memory`` + ``--memory-swap``
        Equal values mean no swap, so the ceiling is real rather than a threshold
        the kernel can push work past into swap.
    ``--pids-limit``
        Bounds fork bombs, which a job object bounds only indirectly.
    ``--cap-drop ALL`` + ``no-new-privileges``
        A snippet needs no capabilities, so it gets none and cannot acquire any.
    ``--user``
        An unprivileged uid even if the image would have started as root.

    Cost, stated honestly: container start is 100 ms–1 s per run. That is
    acceptable for a queue-driven execution but ruins interactive node-by-node
    debugging, which is why the default is still the local runner and this is
    opted into per deployment.
    """

    image: str = DEFAULT_SANDBOX_IMAGE
    memory_limit: int = DEFAULT_MEMORY_LIMIT
    cpus: float = 1.0
    pids_limit: int = 64
    network: str = "none"
    read_only: bool = True
    tmpfs: str = "/tmp"
    user: str = "65534:65534"
    workdir: str = "/workspace"
    docker: str = "docker"
    probe_timeout: float = 10.0

    # ------------------------------------------------------------------ parts
    def entrypoint(self) -> tuple[str, ...]:
        """The command the container runs, derived from :attr:`workdir`."""
        return (
            "python3",
            "-I",
            "-c",
            CONTAINER_BOOTSTRAP.format(root=self.workdir),
        )
    def command(self, name: str) -> list[str]:
        """The ``docker run`` argv for one execution.

        Separate from :meth:`run` so the flags can be asserted without a daemon.
        That matters here: the whole point of these options is what they say, and
        a test that needs Docker to observe them is a test that never runs in CI.
        """
        memory_mb = max(1, self.memory_limit // (1024 * 1024))
        argv = [
            self.docker,
            "run",
            "--rm",
            "--interactive",
            "--name",
            name,
            "--network",
            self.network,
            "--memory",
            f"{memory_mb}m",
            # Equal to --memory: no swap, so the ceiling cannot be evaded by
            # paging the working set out.
            "--memory-swap",
            f"{memory_mb}m",
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            self.user,
            "--workdir",
            self.workdir,
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
        ]
        if self.read_only:
            argv += [
                "--read-only",
                "--tmpfs",
                f"{self.tmpfs}:rw,noexec,nosuid,size=16m,mode=1777",
            ]
        argv.append(self.image)
        argv.extend(self.entrypoint())
        return argv

    # ------------------------------------------------------------------ probe
    async def available(self) -> tuple[bool, str]:
        """Whether the daemon can actually run a container, and why not.

        Checked per call rather than cached: a daemon that starts mid-session
        should start working without a restart, and a daemon that dies should
        start failing closed.
        """
        if shutil.which(self.docker) is None and not os.path.exists(self.docker):
            return False, f"the docker CLI was not found ({self.docker})"
        try:
            process = await asyncio.create_subprocess_exec(
                self.docker,
                "version",
                "--format",
                "{{.Server.Version}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return False, f"the docker CLI could not be started ({exc})"

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.probe_timeout
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                process.kill()
            return False, f"the docker daemon did not answer within {self.probe_timeout:.0f}s"

        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="replace").strip()
            return False, f"the docker daemon is unreachable ({detail[:200]})"
        return True, ""

    async def _stop(self, name: str) -> None:
        """Remove a container that outlived its ``docker run``.

        Best-effort: the usual outcome is "no such container", because ``--rm``
        already cleaned up. It matters only on the timeout path, where the
        client is killed and would otherwise leave a snippet spinning.
        """
        with contextlib.suppress(Exception):
            process = await asyncio.create_subprocess_exec(
                self.docker,
                "kill",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=5)

    # -------------------------------------------------------------------- run
    async def run(
        self,
        code: str,
        *,
        inputs: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        vars_: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> CodeResult:
        # The AST guard runs here as well as inside the child. Cheap, and it
        # means a snippet that cannot possibly be accepted never costs a
        # container start.
        problems = check_code(code)
        if problems:
            return CodeResult(error=f"SandboxViolation: {'; '.join(problems[:4])}")

        ok, reason = await self.available()
        if not ok:
            # Fail closed. Running the snippet locally instead would be a silent
            # downgrade from "isolated" to "trusted", which is precisely the
            # failure an operator would never notice.
            return CodeResult(
                error=(
                    f"Sandbox unavailable: {reason}. The snippet was NOT run — "
                    "no fallback to local execution is performed. Start the "
                    "daemon, or set SANDBOX_RUNNER=local to accept local "
                    "execution explicitly."
                )
            )

        payload = json.dumps(
            {
                "code": code,
                "inputs": inputs or {},
                "state": state or {},
                "vars": vars_ or {},
                "params": params or {},
            },
            default=str,
        ).encode("utf-8")

        name = f"agent-scaffold-sandbox-{uuid.uuid4().hex[:12]}"
        process = await asyncio.create_subprocess_exec(
            *self.command(name),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=5)
            await self._stop(name)
            return CodeResult(
                error=(
                    f"TimeoutError: code did not finish within {timeout:.0f}s and "
                    "its container was killed."
                )
            )

        detail = (stderr or b"").decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            # 137 is the conventional "killed" code, which is how a memory
            # refusal by the kernel surfaces: the interpreter never gets to
            # report anything of its own.
            if process.returncode == 137:
                return CodeResult(
                    error=(
                        f"MemoryError: the snippet exceeded its "
                        f"{self.memory_limit // (1024 * 1024)} MB limit and was "
                        "terminated."
                    ),
                    stderr=detail[:2000],
                )
            return CodeResult(
                error=(
                    f"Sandbox container exited with code {process.returncode}"
                    + (f": {detail[:500]}" if detail else "")
                ),
                stderr=detail[:2000],
            )

        try:
            body = json.loads((stdout or b"").decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError:
            snippet = (stdout or b"").decode("utf-8", errors="replace")[:500]
            return CodeResult(error=f"Sandbox returned an unreadable result: {snippet}")

        return CodeResult(
            value=body.get("value"),
            stdout=body.get("stdout") or "",
            error=body.get("error"),
            stderr=body.get("stderr") or "",
        )


def build_sandbox_runner(settings: Settings | None = None) -> SandboxRunner:
    """Construct the configured runner.

    Raises :class:`SandboxConfigError` for an unrecognised ``SANDBOX_RUNNER``.
    See that class for why this is not a fallback.
    """
    settings = settings or get_settings()
    kind = (settings.sandbox_runner or "local").strip().lower()
    memory_limit = int(settings.sandbox_memory_bytes or 0) or DEFAULT_MEMORY_LIMIT

    if kind == "local":
        return LocalSandboxRunner(
            mode=(settings.sandbox_mode or "").strip().lower() or None,
            memory_limit=memory_limit,
        )
    if kind == "container":
        return ContainerSandboxRunner(
            image=settings.sandbox_image or DEFAULT_SANDBOX_IMAGE,
            memory_limit=memory_limit,
        )
    raise SandboxConfigError(
        f"SANDBOX_RUNNER={kind!r} is not a known runner. "
        "Use 'local' (child process on this host) or 'container' (Docker)."
    )


#: Process-wide runner. Swapped in tests, and the seam where a per-tenant or
#: remote runner plugs in without touching a node.
_runner: SandboxRunner | None = None


def get_sandbox_runner() -> SandboxRunner:
    global _runner
    if _runner is None:
        _runner = build_sandbox_runner()
    return _runner


def set_sandbox_runner(runner: SandboxRunner | None) -> None:
    """Override the process runner. Passing ``None`` restores the configured one."""
    global _runner
    _runner = runner


__all__ = [
    "CONTAINER_BOOTSTRAP",
    "DEFAULT_SANDBOX_IMAGE",
    "CodeResult",
    "ContainerSandboxRunner",
    "LocalSandboxRunner",
    "SandboxConfigError",
    "SandboxRunner",
    "build_sandbox_runner",
    "get_sandbox_runner",
    "set_sandbox_runner",
]
