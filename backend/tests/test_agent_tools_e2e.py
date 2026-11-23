"""End-to-end: an exported agent with its own business tool, run standalone.

The claim under test is the one task 31 exists for: a business agent can carry a
tool that calls *its own* system, and that tool travels in the package.

Runs the package as a separate process with a cleared environment, because an
in-process check would resolve the platform's own imports and prove nothing.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

PYTHON = sys.executable
BACKEND = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND / "app"


def _clean_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "LLM_API_KEY": "",
        "LLM_BASE_URL": "https://api.openai.com/v1",
    }


def _business_tool() -> str:
    """A tool that behaves like a customer's: calls their API, needs a token."""
    return '''"""Query the order service. This agent's own business capability."""

from __future__ import annotations

import os
from typing import Any

from app.tools.registry import ToolDef, register_tool


async def _query_order(order_id: str) -> dict[str, Any]:
    # A real one would call the customer's API through the egress policy. The
    # shape is what matters here: credentials come from the environment.
    token = os.environ.get("ORDER_API_TOKEN", "")
    return {
        "order_id": order_id,
        "token_present": bool(token),
        "status": "shipped",
    }


register_tool(
    ToolDef(
        name="query_order",
        label="Query order",
        label_zh="查询订单",
        description="Look up one order by id.",
        description_zh="按订单号查询一个订单。",
        parameters={
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
        fn=_query_order,
        dangerous=False,
    )
)
'''


def _write_agent(root: pathlib.Path) -> None:
    from app.runtime.export import build_export, write_export
    from app.graph.schema import WorkflowGraph

    graph = WorkflowGraph(
        id="wf-tools-e2e",
        name="Tool probe",
        nodes=[
            {"id": "start", "type": "start", "label": "Start", "config": {"input_schema": {}}},
            {
                "id": "ask",
                "type": "prompt",
                "label": "Ask",
                "config": {"template": "ORDER {{ vars.start.out.order_id }}"},
            },
            {"id": "end", "type": "end", "label": "End", "config": {"output_label": "result"}},
        ],
        edges=[
            {"id": "e1", "source": "start", "sourceHandle": "out", "target": "ask", "targetHandle": "in"},
            {"id": "e2", "source": "ask", "sourceHandle": "out", "target": "end", "targetHandle": "in"},
        ],
        # The tool travels in the definition, which is how the canvas writes it.
        # Hand-writing the files afterwards would produce a package whose
        # `tools_hash` recorded nothing, so selfcheck could not tell an edited
        # tool from an unedited one — the very thing it exists to detect.
        tools={"query_order": _business_tool()},
    )
    write_export(build_export(graph, APP_ROOT), root)


def _run(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "main.py", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
        env=_clean_env(),
    )


def test_the_agent_loads_its_own_tool_and_runs():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "agent"
        _write_agent(root)

        print("=== selfcheck (loads the tool) ===")
        check = _run(root, "selfcheck")
        print(check.stdout.strip())
        assert "RESULT=PASS" in check.stdout, check.stdout + check.stderr

        print("=== run ===")
        run = _run(root, "run", "--input", json.dumps({"order_id": "A-1"}))
        print(run.stdout.strip() or run.stderr.strip())
        assert run.returncode == 0, run.stderr
        # The graph itself does not call the tool (it is a prompt node); what is
        # demonstrated is that the package loads and runs *with* the tool present.
        assert json.loads(run.stdout)["result"] == "ORDER A-1"

        print("=== the tool is registered, and in isolation ===")
        probe = subprocess.run(
            [
                PYTHON,
                "-c",
                (
                    "import sys, pathlib;"
                    "sys.path.insert(0, str(pathlib.Path('.').resolve() / 'vendor' / 'runtime'));"
                    "sys.path.insert(0, str(pathlib.Path('.').resolve()));"
                    "import main; main.prepare();"
                    "from app.tools.registry import get_tool_registry;"
                    "import json;"
                    "print(json.dumps(sorted(t.name for t in get_tool_registry().list())))"
                ),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=180,
            env=_clean_env(),
        )
        print(probe.stdout.strip() or probe.stderr.strip())
        assert probe.returncode == 0, probe.stderr
        # Scanned rather than taken from the end: `load_tools_from` writes WARNINGs
        # to stdout for a bad schema, and the subprocess's text mode emits CRLF so
        # `splitlines()` can yield an empty trailing entry. The probe prints JSON
        # on purpose — the first version printed a Python list, which is not
        # `json.loads`-able, and the assertion was what noticed.
        names = next(
            json.loads(line)
            for line in probe.stdout.splitlines()
            if line.strip().startswith("[")
        )
        assert "query_order" in names, f"the agent's own tool was not loaded: {names}"
        # Registered in the agent's registry only. The platform's built-ins are not
        # in the package, so a name from them appearing here would mean the loader
        # read a store that is not the agent's.
        assert "http_request" not in names, (
            f"the agent's registry picked up the host's tools: {names}"
        )


def test_a_broken_tool_stops_the_agent_instead_of_shrinking_it():
    """The failure mode that matters: a missing tool that looks like a model that
    will not call it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "agent"
        _write_agent(root)

        broken = root / "tools" / "broken.py"
        broken.write_text("this is not python at all\n", encoding="utf-8")
        init = root / "tools" / "__init__.py"
        init.write_text(
            init.read_text(encoding="utf-8") + "\nfrom . import broken  # noqa: F401\n",
            encoding="utf-8",
        )

        result = _run(root, "run", "--input", json.dumps({}))
        assert result.returncode != 0, "a broken tool did not stop the agent"
        assert "FATAL" in result.stderr, result.stderr
        print("stopped as designed:", result.stderr.strip().splitlines()[0])
