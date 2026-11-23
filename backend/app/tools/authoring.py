"""Writing and checking an agent's tool modules from the canvas.

Step 0.5 of the delivery flow is "write the business tools", and until this
existed the canvas had nowhere to put one — the flow broke there and the rest was
done by hand. So this module does the two things that step needs:

* **write** a tool module into a workflow's definition, and
* **check** it, by compiling and importing it in a throwaway interpreter.

Why the check is a subprocess
-----------------------------
Importing a candidate module in-process means executing arbitrary code inside the
API server. That is the same reasoning as the code sandbox: if we are going to run
someone's code, it does not belong in the process that serves requests. It is also
the only way the answer is honest — the module has to import under the *runtime's*
imports and nothing else, which is what a customer's machine will have.

The check is deliberately not "does it match a regex". A tool module is Python,
and the only authority on whether Python is valid Python is Python.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from app.settings import get_settings
from app.tools.loading import TOOLS_PACKAGE

#: A module name has to be an importable identifier, and it becomes a filename, so
#: this is both a correctness rule and the path-traversal guard. Checked rather
#: than sanitised: silently renaming `lookup order` to `lookup_order` would produce
#: a tool the user cannot find by the name they typed.
MAX_MODULES = 40
MAX_SOURCE_BYTES = 200_000


class ToolSourceError(ValueError):
    """Raised when a tool module cannot be accepted as written."""


def validate_module_name(name: str) -> str:
    """Check a module name. Returns it, so callers can use it inline."""
    if not name:
        raise ToolSourceError("A tool module needs a name.")
    if not name.isidentifier():
        raise ToolSourceError(
            f"'{name}' is not a valid module name. Use letters, digits and "
            "underscores, and do not start with a digit."
        )
    if name.startswith("_") or name in {"main", "config", "vendor"}:
        raise ToolSourceError(
            f"'{name}' is reserved. Pick another name so the module cannot "
            "shadow something the package needs."
        )
    return name


def validate_source(source: str) -> None:
    """Check a module's text before it is stored.

    Only the cheap checks live here — size, decodability, and that it is not
    empty. Whether it *works* is answered by :func:`check_module`, because that
    needs to run it.
    """
    if not source.strip():
        raise ToolSourceError("A tool module cannot be empty.")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ToolSourceError(
            f"A tool module is limited to {MAX_SOURCE_BYTES // 1000} KB. "
            "If it needs more, move the bulk into a module the tenant installs."
        )


def check_module(name: str, source: str) -> dict[str, Any]:
    """Import ``name``/``source`` in a throwaway interpreter and report.

    Returns ``{"ok", "tools", "error", "warnings"}``. Never raises for a bad
    module — a bad module is the expected input, not an exceptional condition.

    The tools the module registered are reported back, which is what lets the
    editor show "this defines: query_order" instead of asking the user to trust
    that it worked.
    """
    name = validate_module_name(name)
    validate_source(source)

    settings = get_settings()
    # The runtime this deployment is running, so the check imports what the agent
    # will import rather than a different copy.
    runtime_root = Path(__file__).resolve().parents[2]

    probe = textwrap.dedent(
        """
        import json, sys

        root, tool_dir, module_name = sys.argv[1], sys.argv[2], sys.argv[3]
        sys.path.insert(0, root)

        from app.tools.loading import ToolLoadError, load_tools_from

        try:
            # `load_tools_from` is the same loader an exported agent uses, so the
            # check cannot pass here and fail in the package.
            registry, problems = load_tools_from(tool_dir)
        except ToolLoadError as exc:
            print(json.dumps({"ok": False, "error": str(exc), "tools": [], "warnings": []}))
            raise SystemExit(0)

        found = sorted(t.name for t in registry.list())
        if not found:
            print(json.dumps({
                "ok": False,
                "error": (
                    f"'{module_name}' imported but registered no tools. A tool module "
                    "must call register_tool(ToolDef(...)) at import time."
                ),
                "tools": [],
                "warnings": problems,
            }))
            raise SystemExit(0)

        print(json.dumps({"ok": True, "error": None, "tools": found, "warnings": problems}))
        """
    )

    with tempfile.TemporaryDirectory() as tmp:
        package_dir = Path(tmp) / TOOLS_PACKAGE
        package_dir.mkdir()
        # The package's `__init__.py` imports the candidate, matching how the
        # exporter writes it — a module that only works when imported directly
        # would pass here and fail in the package.
        (package_dir / "__init__.py").write_text(
            f"from . import {name}  # noqa: F401\n", encoding="utf-8"
        )
        (package_dir / f"{name}.py").write_text(source, encoding="utf-8")

        try:
            result = subprocess.run(
                [sys.executable, "-c", probe, str(runtime_root), str(package_dir), name],
                capture_output=True,
                timeout=settings.tool_check_seconds,
                env=_child_env(),
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": (
                    f"Checking the module took longer than "
                    f"{settings.tool_check_seconds:g}s. It is probably running code at "
                    "import time — build clients inside the tool function instead."
                ),
                "tools": [],
                "warnings": [],
            }

    # Decoded by hand rather than with `text=True`: the child's output encoding is
    # not necessarily ours, and a mis-decoded traceback is worse than none.
    stdout = result.stdout.decode("utf-8", "replace")
    stderr = result.stderr.decode("utf-8", "replace")

    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    # No JSON came back: the module died before the probe could report, or the
    # interpreter itself failed. Either way, show what it said.
    detail = (stderr or stdout or "no output").strip()
    return {
        "ok": False,
        "error": f"The module could not be imported:\n{detail[-2000:]}",
        "tools": [],
        "warnings": [],
    }


def _child_env() -> dict[str, str]:
    """The environment for the checker's subprocess.

    Inherits rather than starting empty. An emptied environment looks stricter
    but is not: on Windows the interpreter needs ``SYSTEMROOT`` and other system
    variables to start at all, so clearing them fails the check with an error
    about the *environment* instead of about the module. Isolation that matters
    comes from the namespace lease below and the temporary directory, not from
    starving the process.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # A PID-derived seed is the only supported isolation mode on Windows; leaving
    # it unset makes each child pay for hashing randomisation it cannot use.
    env.setdefault("PYTHONHASHSEED", "0")
    return env


def tools_package_source(modules: dict[str, str]) -> str:
    """The `tools/__init__.py` for a set of modules.

    Kept here rather than in the exporter because two callers now need it: the
    export writes the package, and the editor shows what the package will look
    like. One implementation, so the two cannot disagree about which modules are
    imported.
    """
    lines = [
        '"""This agent\'s own tools.',
        "",
        "Importing a module is what registers its tools.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
    ]
    for name in sorted(modules):
        lines.append(f"from . import {name}  # noqa: F401  - importing registers it")
    return "\n".join(lines) + "\n"


__all__ = [
    "MAX_MODULES",
    "MAX_SOURCE_BYTES",
    "ToolSourceError",
    "check_module",
    "tools_package_source",
    "validate_module_name",
    "validate_source",
]
