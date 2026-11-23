"""Code node — the deliberate escape hatch.

Every workflow tool eventually hits a case the built-in nodes cannot express.
Rather than growing a node type for each one, this node runs a small Python
snippet with the graph's data in scope.

.. warning::
   Code nodes execute arbitrary Python. Which *kind* of execution that is depends
   on the deployment: :mod:`app.runtime.runner` decides, and the default is a
   resource-limited child process on the same host. They are gated behind a
   per-node ``trusted`` flag and the global ``CODE_NODE_ENABLED`` setting, and the
   local sandbox removes the obvious footguns — but it is **not** a security
   boundary, because it does not isolate the filesystem or the network. Set
   ``SANDBOX_RUNNER=container`` for workloads you did not write yourself. See
   `docs/current/security.md#sec3` before exposing this to anyone else.
"""

from __future__ import annotations

from typing import Any

from app.graph.schema import (
    BooleanParam,
    CodeParam,
    InputPort,
    NodeCategory,
    NodeTypeSpec,
    OutputPort,
    PortType,
)
from app.nodes.base import BaseNode, NodeContext
from app.nodes.registry import register_node
from app.runtime.runner import get_sandbox_runner
from app.runtime.sandbox import CODE_CONTRACT_HELP, CODE_CONTRACT_HELP_ZH

DEFAULT_SNIPPET = '''\
# `inputs` holds values arriving on this node's handles.
value = inputs.get("in", "")

# `vars` holds every earlier node's output, keyed by node id.
# e.g. vars["node_1"]["out"]

# Return by assigning `result`:
result = {
    "value": value,
    "length": len(value) if hasattr(value, "__len__") else 0,
}
'''

CODE_SPEC = NodeTypeSpec(
    type="code",
    label="Code",
    label_zh="代码",
    category=NodeCategory.DATA,
    description="Run a Python snippet with the graph's data in scope.",
    description_zh="在可直接访问图数据的环境里运行一段 Python 代码。",
    icon="terminal",
    color="#64748b",
    requires_trust=True,
    inputs=[
        InputPort(
            id="in",
            label="Input",
            label_zh="输入",
            type=PortType.ANY,
            description="Available inside the snippet as inputs['in'].",
            description_zh="在代码里通过 inputs['in'] 获取。",
        )
    ],
    outputs=[
        OutputPort(id="out", label="Result", label_zh="结果", type=PortType.ANY),
        OutputPort(id="stdout", label="Stdout", label_zh="标准输出", type=PortType.TEXT),
    ],
    params=[
        CodeParam(
            name="code",
            label="Python",
            label_zh="Python 代码",
            description=CODE_CONTRACT_HELP,
            description_zh=CODE_CONTRACT_HELP_ZH,
            language="python",
            rows=16,
            default=DEFAULT_SNIPPET,
            required=True,
        ),
        BooleanParam(
            name="fail_on_error",
            label="Fail the run on error",
            label_zh="出错时让运行失败",
            description="When off, errors are captured and passed downstream instead.",
            description_zh="关闭时，错误会被捕获并继续传给下游。",
            default=True,
        ),
    ],
    ai_notes="""\
The escape hatch, with hard walls. Writing a snippet that the sandbox refuses
produces a run that fails at that node and nothing else — so know the walls
before choosing this node.

**Available in scope:** `inputs` (this node's input handles), `vars` (every
earlier node's output, keyed by node id), `params` (this node's config), `state`.
Return by assigning `result`, or by defining `def run(inputs, state, vars, params)`.

**What it cannot do:**
* No `open`, no file access — and therefore **no database, no writing results to
  disk**. If the job is "record this somewhere durable", that is a Tool node
  calling a registered tool, not a Code node.
* No `eval`, `exec`, `compile`, `getattr`/`setattr`, no `__import__`, no dunder
  traversal (`().__class__`). These are removed from the builtins outright.
* Imports are limited to a fixed allowlist: json, math, re, datetime,
  collections, itertools, functools, textwrap, random, statistics, string,
  typing, uuid, hashlib, base64, urllib.parse. No `requests`, no `sqlite3`,
  no `bs4`.
* No network. Fetching a URL is a Tool node's job (`http_request`).

**Trust.** The node will not run until `trusted` is true, and the generator
deliberately does not set it. See the trust rule in the system prompt: say so
plainly in your finish summary instead of arming it.

**Reading a previous pass.** Inside a loop, `vars[<this node's own id>]` still
holds this node's *previous* output when it runs again, which is how a snippet
accumulates across iterations. Give it a default — on the first pass the key is
absent.

**Prefer an ordinary node when one fits.** `json_query` reads a dotted path,
`calculator` evaluates an expression, `text_stats` measures text. Reaching for a
Code node to do those adds a trust prompt the user has to understand for no gain.\
""",
)


@register_node
class CodeNode(BaseNode):
    spec = CODE_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        if not ctx.settings.code_node_enabled:
            raise ValueError(
                "Code nodes are disabled. Set CODE_NODE_ENABLED=true in .env to use them."
            )
        if not ctx.node.trusted:
            raise ValueError(
                f"Code node '{ctx.node.label or ctx.node.id}' is not marked as trusted. "
                "Enable 'trusted' in the Inspector to allow it to run."
            )

        code = ctx.config.get("code") or ""
        # Through the capability layer, not the implementation. Which runner this
        # is — a child process, a container, something remote — is a deployment
        # decision made in `app/runtime/runner.py`; this node must not care.
        result = await get_sandbox_runner().run(
            code,
            inputs=ctx.inputs,
            state=ctx.state,
            vars_=ctx.vars,
            params=ctx.config,
            timeout=ctx.settings.node_timeout_seconds,
        )

        if result.stdout.strip():
            await ctx.log(result.stdout.strip()[:2000], "info")

        if not result.ok:
            message = f"Code node failed: {result.error}"
            await ctx.log(message, "error")
            if ctx.config.get("fail_on_error", True):
                raise ValueError(message)

        await ctx.log("Snippet finished.", "info")
        return {
            "out": result.value,
            "stdout": result.stdout,
        }


__all__ = ["CODE_SPEC", "CodeNode", "DEFAULT_SNIPPET"]
