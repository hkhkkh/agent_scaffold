"""Code-node sandbox.

What the boundary actually is
-----------------------------

Two layers, and it matters which one is load-bearing:

1. **The subprocess is the resource boundary.** It can be killed, it has a hard
   memory ceiling, and a crash or an endless loop cannot take the API process
   down with it. This layer is enforced by the operating system and cannot be
   undone by the code inside — a Windows job object and a POSIX ``setrlimit``
   both refuse to be relaxed from within the constrained process.

2. **The AST guard is a speed bump.** It blocks dunder attribute traversal
   (``().__class__.__mro__``) and drops the ``getattr``/``setattr`` family from
   the builtins, which closes the textbook escape. It is *not* a security
   boundary and should not be described as one: filtering Python with Python is
   bypassable by construction, and anyone claiming otherwise has not tried hard
   enough.

What is therefore **not** protected: the filesystem, and the network. Code in a
Code node runs as the backend's user and can read and write what that user can.
Filesystem and network isolation require the container profile — see
``docs/log/phase-1-1-sandbox.md#sb8``. That is a documented limitation of the
local profile, and it is exactly why ``requires_trust`` exists:
ticking "Trusted" means "I accept this snippet runs with my user's filesystem
access".

History
-------

The previous implementation ran ``exec`` in a worker thread of the API process
and relied on ``asyncio.wait_for`` for the timeout. That combination cannot stop
a CPU-bound loop — ``wait_for`` abandons the *await* while the thread keeps a
core pinned forever — and there was no memory limit at all. The subprocess closes
both holes.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import io
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#: Modules a Code node may import. Add to this list as needed.
ALLOWED_MODULES = frozenset(
    {
        "json",
        "math",
        "re",
        "datetime",
        "collections",
        "itertools",
        "functools",
        "textwrap",
        "random",
        "statistics",
        "string",
        "typing",
        "uuid",
        "hashlib",
        "base64",
        "urllib.parse",
    }
)

#: Builtin names a Code node may use.
#:
#: An **allowlist**, deliberately: a denylist over the Python builtins is a losing
#: game, because the set of dangerous names is open-ended and every new CPython
#: release can add to it. The names a snippet legitimately needs for data
#: wrangling are few and stable, so enumerating them is both safer and shorter.
#:
#: Note what is absent and why: ``getattr``/``setattr``/``hasattr``/``delattr``
#: (they reach a dunder attribute through a concatenated string, which no AST
#: check can see), ``open``, ``eval``/``exec``/``compile``, and ``__import__``
#: (replaced by the guarded version below).
_ALLOWED_BUILTINS = frozenset(
    {
        "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
        "callable", "chr", "dict", "divmod", "enumerate", "filter", "float",
        "format", "frozenset", "hash", "hex", "int", "isinstance",
        "iter", "len", "list", "map", "max", "min", "next", "oct", "ord", "pow",
        "print", "range", "repr", "reversed", "round", "set", "slice",
        "sorted", "str", "sum", "tuple", "type", "zip", "True", "False", "None",
        "Ellipsis", "NotImplemented",
        "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
        "AttributeError", "RuntimeError", "StopIteration", "ZeroDivisionError",
        "ArithmeticError", "LookupError", "NotImplementedError", "AssertionError",
    }
)

#: Names removed from the namespace *after* the allowlist is applied.
#:
#: Currently disjoint from :data:`_ALLOWED_BUILTINS`, so this subtracts nothing at
#: runtime. It is kept — and tested — as the safety net beneath the allowlist:
#: someone adding ``"getattr"`` to the allowlist in a moment of enthusiasm gets it
#: silently removed here rather than silently granted. A blocklist is a poor
#: primary defence and a good second one.
_BLOCKED_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "dir",
        "exit",
        "quit",
        "help",
        "memoryview",
        "object",
        "getattr",
        "setattr",
        "hasattr",
        "delattr",
        "issubclass",
        "classmethod",
        "staticmethod",
        "super",
        "property",
    }
)

#: Directory the child must be able to import ``app`` from.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Default memory ceiling for one Code-node run, in bytes. Sized to leave room
#: for the interpreter itself (~30 MB) plus a generous working set.
DEFAULT_MEMORY_LIMIT = 512 * 1024 * 1024


class SandboxViolation(Exception):
    """Raised when a snippet is rejected before it runs."""


class SandboxTimeout(Exception):
    """Raised when a snippet exceeds its wall-clock budget."""


# ---------------------------------------------------------------------------
# AST guard
# ---------------------------------------------------------------------------


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


class _Guard(ast.NodeVisitor):
    """Rejects the syntactic shapes used to climb out of a sandbox.

    Deliberately narrow: it blocks dunder *names* and dunder *attributes*, which
    is what every naive escape needs, and leaves ordinary code alone. It does not
    try to be complete, because completeness is not achievable at this layer.
    """

    def __init__(self) -> None:
        self.problems: list[str] = []

    def visit_Name(self, node: ast.Name) -> None:
        if _is_dunder(node.id):
            self.problems.append(
                f"line {node.lineno}: '{node.id}' is not available in a Code node"
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _is_dunder(node.attr):
            self.problems.append(
                f"line {node.lineno}: attribute '{node.attr}' is not accessible "
                "in a Code node"
            )
        self.generic_visit(node)


def check_code(code: str) -> list[str]:
    """Return the guard's complaints about ``code``; empty means it may run."""
    try:
        tree = ast.parse(code, filename="<code-node>", mode="exec")
    except SyntaxError as exc:
        return [f"SyntaxError: {exc.msg} (line {exc.lineno})"]

    guard = _Guard()
    guard.visit(tree)
    return guard.problems


def compile_guarded(code: str) -> Any:
    """Parse and compile, refusing snippets that trip the guard."""
    problems = check_code(code)
    if problems:
        raise SandboxViolation("; ".join(problems[:4]))
    return compile(code, "<code-node>", "exec")


def _guarded_import(
    name: str,
    globals_: dict[str, Any] | None = None,
    locals_: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    root = name.split(".")[0]
    if name not in ALLOWED_MODULES and root not in ALLOWED_MODULES:
        raise ImportError(
            f"Module '{name}' is not allowed in a Code node. "
            f"Allowed: {', '.join(sorted(ALLOWED_MODULES))}"
        )
    return builtins.__import__(name, globals_, locals_, fromlist, level)


def safe_builtins() -> dict[str, Any]:
    """Curated builtin namespace for Code nodes.

    Allowlist first, then the blocklist subtracted from the result. The second
    step is a net rather than the mechanism — see :data:`_BLOCKED_NAMES` — and
    exists so that widening the allowlist cannot quietly widen the sandbox.
    """
    namespace: dict[str, Any] = {
        name: getattr(builtins, name)
        for name in _ALLOWED_BUILTINS
        if hasattr(builtins, name)
    }
    namespace["__import__"] = _guarded_import
    for blocked in _BLOCKED_NAMES:
        namespace.pop(blocked, None)
    return namespace


@dataclass
class CodeResult:
    """Outcome of one Code-node execution."""

    value: Any = None
    stdout: str = ""
    error: str | None = None
    stderr: str = ""
    namespace: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


#: Documentation string injected into the AI prompt and shown in the Inspector
#: so authors know exactly what is in scope.
CODE_CONTRACT_HELP = """\
Available inside the code:
  inputs : dict  - values arriving on this node's input handles, e.g. inputs["in"]
  state  : dict  - full graph state (read-only by convention)
  vars   : dict  - outputs of every node so far, e.g. vars["node_1"]["out"]
  params : dict  - this node's resolved configuration

Return a value either by assigning `result`, or by defining:

    def run(inputs, state, vars, params):
        return ...

`result` wins if both are present.
Allowed imports: %s
""" % ", ".join(sorted(ALLOWED_MODULES))

#: Same contract, in Chinese. Kept as a parallel constant rather than generated,
#: because the layout (aligned columns, a code block) is part of what makes it
#: readable and a mechanical translation would lose it.
CODE_CONTRACT_HELP_ZH = """\
代码里可以直接使用：
  inputs : dict  - 从本节点输入接口传进来的值，例如 inputs["in"]
  state  : dict  - 完整的图状态（约定上只读）
  vars   : dict  - 目前所有节点的输出，例如 vars["node_1"]["out"]
  params : dict  - 本节点解析后的配置

两种方式返回结果，任选其一：直接给 `result` 赋值，或者定义：

    def run(inputs, state, vars, params):
        return ...

两者同时存在时以 `result` 为准。
允许导入的模块：%s
""" % ", ".join(sorted(ALLOWED_MODULES))


# ---------------------------------------------------------------------------
# In-process runner (development fallback)
# ---------------------------------------------------------------------------


def _exec_sync(
    code: str,
    scope: dict[str, Any],
    inputs: dict[str, Any],
    state: dict[str, Any],
    vars_: dict[str, Any],
    params: dict[str, Any],
) -> CodeResult:
    """Run a snippet in this process. Retained for tests and ``mode=inprocess``."""
    buffer = io.StringIO()

    namespace = dict(scope)
    namespace.update(
        {"inputs": inputs, "state": state, "vars": vars_, "params": params}
    )

    try:
        compiled = compile_guarded(code)
        with contextlib.redirect_stdout(buffer):
            exec(compiled, namespace)  # noqa: S102 - see module docstring

            produced: Any = namespace.get("result", None)
            runner: Callable[..., Any] | None = namespace.get("run")
            if callable(runner):
                try:
                    produced = runner(inputs, state, vars_, params)
                except TypeError:
                    produced = runner(inputs, state)

        return CodeResult(value=produced, stdout=buffer.getvalue(), namespace=namespace)
    except SandboxViolation as exc:
        return CodeResult(stdout=buffer.getvalue(), error=f"SandboxViolation: {exc}")
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a run failure
        return CodeResult(
            stdout=buffer.getvalue(),
            error=f"{type(exc).__name__}: {exc}",
            namespace=namespace,
        )


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


def _posix_preexec(memory_limit: int) -> Callable[[], None]:
    """Return a ``preexec_fn`` that caps address space and CPU time.

    Applied between ``fork`` and ``exec``, so it cannot be undone by the child —
    by the time any untrusted code exists, the limits are already in force.
    """

    def apply() -> None:  # pragma: no cover - exercised only on Linux/macOS
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
        # A hard CPU bound independent of the wall clock, so a snippet cannot
        # burn a core forever even if the parent is wedged.
        cpu = int(os.environ.get("SANDBOX_CPU_SECONDS", "30"))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))

    return apply


if sys.platform == "win32":  # pragma: no branch
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    def _kernel32() -> Any:
        return ctypes.WinDLL("kernel32", use_last_error=True)

    def _create_memory_limited_job(memory_limit: int) -> int:
        """Create a job object capping memory, returning its handle.

        A job object is used rather than a self-imposed limit because the child
        cannot loosen it: the ceiling is applied by the kernel to the process,
        not by code running inside it.
        """
        kernel32 = _kernel32()
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(f"CreateJobObjectW failed ({ctypes.get_last_error()})")

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # Deliberately no JOB_OBJECT_LIMIT_ACTIVE_PROCESS. It blocks the second
        # process a venv's launcher stub wants to create, which makes the whole
        # sandbox unusable under `venv_d`. The property it would have bought —
        # "the snippet cannot spawn anything" — is largely covered anyway, since
        # a job member's children inherit the job and are therefore subject to
        # the same memory ceiling and killed with it when the handle closes.
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _JOB_OBJECT_LIMIT_JOB_MEMORY
            # If the API process dies, the job closes and the child goes with it
            # rather than being orphaned mid-snippet.
            | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        info.ProcessMemoryLimit = memory_limit
        info.JobMemoryLimit = memory_limit

        ok = kernel32.SetInformationJobObject(
            wintypes.HANDLE(handle),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(wintypes.HANDLE(handle))
            raise OSError(f"SetInformationJobObject failed ({error})")
        return int(handle)

    def _assign_to_job(job: int, pid: int) -> None:
        kernel32 = _kernel32()
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        process = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
        )
        if not process:
            raise OSError(f"OpenProcess failed ({ctypes.get_last_error()})")
        try:
            if not kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(job), wintypes.HANDLE(process)
            ):
                raise OSError(
                    f"AssignProcessToJobObject failed ({ctypes.get_last_error()})"
                )
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(process))

    def _close_handle(handle: int) -> None:
        _kernel32().CloseHandle(wintypes.HANDLE(handle))


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------


def _child_executable() -> str:
    """Interpreter to launch the child with.

    Prefers the *base* interpreter over a venv's ``python.exe``. On Windows a venv
    executable is a launcher stub that immediately spawns the real interpreter as
    a second process; that extra hop doubles the memory charged against the job
    and is exactly what an active-process limit would reject.

    Nothing is lost by skipping it: the child imports only the standard library
    plus the backend path injected in :data:`_CHILD_BOOTSTRAP`, never the venv's
    site-packages.
    """
    base = getattr(sys, "_base_executable", None)
    if base and base != sys.executable and os.path.exists(base):
        return base
    return sys.executable


def _child_env() -> dict[str, str]:
    """Environment for the child: stripped of credentials.

    A snippet has no business reading the operator's API key out of its own
    environment, so the obvious names are removed before it starts. The child is
    also launched with ``-I``, which ignores ``PYTHON*`` variables entirely —
    that is why the import path is injected in the command rather than exported
    here.
    """
    env = dict(os.environ)
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(key, None)
    return env


#: Bootstrap that puts the backend on ``sys.path`` and runs the child's ``main``.
#:
#: Necessary because ``-I`` (isolated mode) implies ``-P`` on Python 3.11+, so
#: neither the working directory nor ``PYTHONPATH`` contributes to ``sys.path``.
#: Isolation is worth keeping — it drops the user site directory and every
#: ``PYTHON*`` environment variable — so the path is injected explicitly instead.
_CHILD_BOOTSTRAP = (
    "import sys; sys.path.insert(0, {root!r}); "
    "from app.runtime.sandbox_child import main; raise SystemExit(main())"
)


async def run_python_subprocess(
    code: str,
    *,
    inputs: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    vars_: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
    memory_limit: int = DEFAULT_MEMORY_LIMIT,
) -> CodeResult:
    """Execute a snippet in a resource-limited child process.

    The child is constrained *before* it receives the code, so no untrusted
    instruction runs outside the limit.
    """
    problems = check_code(code)
    if problems:
        return CodeResult(error=f"SandboxViolation: {'; '.join(problems[:4])}")

    payload = json.dumps(
        {
            "code": code,
            "inputs": inputs or {},
            "state": state or {},
            "vars": vars_ or {},
            "params": params or {},
        },
        default=str,
    )

    kwargs: dict[str, Any] = {}
    job_handle: int | None = None
    if sys.platform == "win32":
        # Create the job first. Assignment happens immediately after spawn and
        # well before the child's first statement, because the child blocks on
        # stdin — that is the guarantee which makes this ordering safe.
        try:
            job_handle = _create_memory_limited_job(memory_limit)
        except OSError as exc:
            # Fail closed rather than silently running without the ceiling.
            return CodeResult(error=f"Sandbox unavailable: {exc}")
    else:
        kwargs["preexec_fn"] = _posix_preexec(memory_limit)

    process = await asyncio.create_subprocess_exec(
        _child_executable(),
        "-I",  # isolated: ignore PYTHON* env vars and the user site directory
        "-c",
        _CHILD_BOOTSTRAP.format(root=str(_BACKEND_ROOT)),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_BACKEND_ROOT),
        env=_child_env(),
        **kwargs,
    )

    if job_handle is not None:
        try:
            _assign_to_job(job_handle, process.pid)
        except OSError as exc:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            _close_handle(job_handle)
            return CodeResult(error=f"Sandbox unavailable: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # The whole point of the subprocess: this actually stops the work.
        process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        return CodeResult(
            error=(
                f"TimeoutError: code did not finish within {timeout:.0f}s and was "
                "terminated."
            )
        )
    finally:
        if job_handle is not None:
            _close_handle(job_handle)

    detail = (stderr or b"").decode("utf-8", errors="replace").strip()

    if process.returncode not in (0, None):
        if "MemoryError" in detail or process.returncode not in (1, 2):
            # An abnormal exit is how a job-object memory refusal surfaces: the
            # interpreter dies before it can report anything of its own.
            return CodeResult(
                error=(
                    f"MemoryError: the snippet exceeded its "
                    f"{memory_limit // (1024 * 1024)} MB limit and was terminated."
                ),
                stderr=detail[:2000],
            )
        return CodeResult(
            error=f"Sandbox exited with code {process.returncode}",
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


async def run_python(
    code: str,
    *,
    inputs: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    vars_: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
    mode: str | None = None,
    memory_limit: int | None = None,
) -> CodeResult:
    """Execute user Python. Subprocess by default; see the module docstring.

    ``mode`` accepts ``"subprocess"`` (default) or ``"inprocess"``. The in-process
    path exists for tests that need the live namespace and for environments where
    spawning is impossible; it is not safe against untrusted code and is never
    selected automatically.

    ``memory_limit`` is an explicit injection point rather than an environment
    read, because the resource policy belongs to the runner that was configured
    (:mod:`app.runtime.runner`). ``None`` falls back to the previous behaviour:
    ``SANDBOX_MEMORY_BYTES``, then :data:`DEFAULT_MEMORY_LIMIT`.
    """
    if not (code or "").strip():
        return CodeResult(error="Code is empty.")

    chosen = (mode or os.environ.get("SANDBOX_MODE") or "subprocess").strip().lower()

    if chosen == "inprocess":
        scope: dict[str, Any] = {"__builtins__": safe_builtins()}
        result = await asyncio.wait_for(
            asyncio.to_thread(
                _exec_sync,
                code,
                scope,
                inputs or {},
                state or {},
                vars_ or {},
                params or {},
            ),
            timeout=timeout,
        )
    else:
        if memory_limit is None:
            memory_limit = int(
                os.environ.get("SANDBOX_MEMORY_BYTES") or DEFAULT_MEMORY_LIMIT
            )
        result = await run_python_subprocess(
            code,
            inputs=inputs,
            state=state,
            vars_=vars_,
            params=params,
            timeout=timeout,
            memory_limit=memory_limit,
        )

    # A coroutine result cannot cross a process boundary; only the in-process
    # path can finish one.
    if result.ok and asyncio.iscoroutine(result.value):
        if chosen == "inprocess":
            try:
                result.value = await asyncio.wait_for(result.value, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                result.error = f"{type(exc).__name__}: {exc}"
        else:
            result.error = (
                "A Code node cannot return a coroutine in the subprocess sandbox. "
                "Do the async work outside the node, or set SANDBOX_MODE=inprocess "
                "(not recommended)."
            )
            result.value = None
    return result


__all__ = [
    "ALLOWED_MODULES",
    "CODE_CONTRACT_HELP",
    "CODE_CONTRACT_HELP_ZH",
    "CodeResult",
    "DEFAULT_MEMORY_LIMIT",
    "SandboxTimeout",
    "SandboxViolation",
    "check_code",
    "compile_guarded",
    "run_python",
    "run_python_subprocess",
    "safe_builtins",
]
