"""Subprocess entry point for Code-node execution.

This module is the *child* half of the sandbox. It is imported by
``python -m app.runtime.sandbox_child`` and never called in-process.

Protocol
--------

The parent writes one JSON object to stdin::

    {"code": ..., "inputs": {...}, "state": {...}, "vars": {...},
     "params": {...}, "max_stdout": 20000, "max_result": 65536}

and reads one JSON object from stdout::

    {"value": ..., "stdout": "...", "error": null, "stderr": ""}

Nothing else may be written to the real stdout, which is why the user's
``print`` output is captured into a buffer and returned inside the payload. A
stray print would corrupt the protocol.

Why the child waits for stdin
-----------------------------

The parent assigns this process to a resource-limited job object *before* sending
the payload, and this module's first act is to block on ``stdin.read()``. That
ordering is what makes the limit race-free: untrusted code cannot start running
until the parent has finished constraining the process that will run it.
Spawning with ``CREATE_SUSPENDED`` would be the other way to close that window,
but retrieving the main thread handle requires going below ``subprocess``;
blocking on stdin achieves the same guarantee in portable Python.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import traceback
from typing import Any

from app.runtime.sandbox import (
    CodeResult,
    SandboxViolation,
    compile_guarded,
    safe_builtins,
)


def _run(code: str, payload: dict[str, Any]) -> CodeResult:
    """Execute the snippet and return a result. Never raises."""
    buffer = io.StringIO()

    # Rebuilt here rather than sent over the wire: the namespace contains live
    # function objects (the guarded import) and cannot be serialised.
    namespace: dict[str, Any] = {"__builtins__": safe_builtins()}
    namespace.update(
        {
            "inputs": payload.get("inputs") or {},
            "state": payload.get("state") or {},
            "vars": payload.get("vars") or {},
            "params": payload.get("params") or {},
        }
    )

    try:
        compiled = compile_guarded(code)
        with contextlib.redirect_stdout(buffer):
            exec(compiled, namespace)  # noqa: S102 - this module *is* the boundary

            produced: Any = namespace.get("result", None)
            runner = namespace.get("run")
            if callable(runner):
                try:
                    produced = runner(
                        namespace["inputs"],
                        namespace["state"],
                        namespace["vars"],
                        namespace["params"],
                    )
                except TypeError:
                    # Tolerate a narrower signature so one-liners still work.
                    produced = runner(namespace["inputs"], namespace["state"])

        return CodeResult(value=produced, stdout=buffer.getvalue())
    except SandboxViolation as exc:
        return CodeResult(
            stdout=buffer.getvalue(),
            error=f"SandboxViolation: {exc}",
        )
    except MemoryError:
        # A dedicated branch because `str(MemoryError())` is the empty string:
        # the generic handler below produced a bare "MemoryError: " that told the
        # author nothing at all. Reaching here means the allocation failed inside
        # the interpreter — when the hard ceiling is hit instead, the kernel kills
        # this process and the parent reports that separately.
        return CodeResult(
            stdout=buffer.getvalue(),
            error=(
                "MemoryError: the snippet ran out of memory. It asked for more "
                "than the sandbox's memory limit allows."
            ),
            stderr=traceback.format_exc(limit=4),
        )
    except BaseException as exc:  # noqa: BLE001 - reported, never propagated
        return CodeResult(
            stdout=buffer.getvalue(),
            error=f"{type(exc).__name__}: {exc}",
            stderr=traceback.format_exc(limit=4),
        )


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        json.dump({"error": f"bad request: {exc}"}, sys.stdout)
        return 2

    result = _run(payload.get("code") or "", payload)

    # The value may hold something JSON cannot encode (a set, a custom object).
    # Falling back to a string keeps the protocol intact; the parent prefers a
    # degraded answer to a crashed child.
    body = {
        "value": result.value,
        "stdout": result.stdout,
        "error": result.error,
        "stderr": result.stderr,
    }
    try:
        text = json.dumps(body, default=str)
    except (TypeError, ValueError):
        text = json.dumps(
            {
                "value": repr(result.value),
                "stdout": result.stdout,
                "error": result.error,
                "stderr": result.stderr,
            }
        )

    max_result = int(payload.get("max_result") or 65_536)
    if len(text) > max_result:
        text = json.dumps(
            {
                "value": None,
                "stdout": result.stdout[: 2000],
                "error": (
                    f"Result too large to return ({len(text)} bytes > "
                    f"{max_result}). Return a smaller value."
                ),
                "stderr": "",
            }
        )

    sys.stdout.write(text)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
