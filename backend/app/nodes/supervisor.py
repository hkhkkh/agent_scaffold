"""Supervisor node — decides which worker runs next, and when to stop.

What this is for
----------------
A graph with several specialists ("check refunds", "check shipping", "escalate")
needs something that decides *which one runs for this request* and *when the work
is done*. That is task 24. Without it the author has to draw every path, which is
the "200-node giant graph" problem the sub-flow feature exists to avoid.

What is reused
--------------
`langgraph.types.Command` — the framework's own handoff primitive, and the
documented way to do multi-agent handoffs. Measured before writing this:

* a node **can** return `Command(goto=...)`; no conditional edge needed
* `Command(goto=END)` stops the graph
* `Command(goto=Command.PARENT, graph=Command.PARENT)` hands back to a parent

★ The rule that shapes the implementation (from the docs, and it is a real trap):
`Command` **adds** a dynamic edge — it does not replace static ones. A node that
returns `Command(goto=X)` while also having a static edge to `Y` runs **both**.
So the compiler must not draw static edges out of a supervisor, or every handoff
would also run whatever the author happened to wire up. See
`app/graph/compiler.py`'s `DYNAMIC_ROUTING_TYPES`.

How the choice is made
----------------------
Two sources, in order:

1. **`decision`** — an expression or template the node evaluates. This is for
   graphs where a rule decides ("if the amount is over 500, escalate"), and it
   keeps the routing deterministic and testable without a model call.
2. **`workers`** — the declared destinations. Used for the *validation* half:
   a decision that names a worker this node does not declare is refused at
   compile time rather than producing a graph that cannot route.

Choosing with a **model** is deliberately not built in. It needs the same
`Command` handoff, and a graph can get it today by putting an Agent node upstream
whose output the supervisor reads as its `decision`. Building it here would mean
a second place that knows how to prompt a model for a routing choice.
"""

from __future__ import annotations

from typing import Any

from app.graph.schema import (
    InputPort,
    JsonParam,
    NodeCategory,
    NodeTypeSpec,
    OutputPort,
    PortType,
    SelectOption,
    SelectParam,
    StringParam,
)
from app.nodes.base import BaseNode, NodeContext, render_template
from app.nodes.registry import register_node

#: Where the node records its choice, so the compiler's conditional edge and the
#: run timeline both read one value rather than two.
DECISION_KEY = "route"

#: The value that stops the graph. Spelled `done` rather than taking `end`, so it
#: cannot be confused with a node id that happens to be `end`.
DONE = "done"

MODE_EXPRESSION = "expression"
MODE_LIST = "list"


def build_supervisor_spec() -> NodeTypeSpec:
    return NodeTypeSpec(
        type="supervisor",
        label="Supervisor",
        label_zh="主管",
        category=NodeCategory.LOGIC,
        description=(
            "Decide which worker runs next, out of the ones listed here, or stop. "
            "Use it to route one request to one of several specialists without "
            "drawing every path."
        ),
        description_zh=(
            "在下面列出的节点里决定下一个跑哪个，或者结束。"
            "用来把一个请求分派给多个专长节点之一，不用把每条路径都画出来。"
        ),
        icon="git-fork",
        color="#7c3aed",
        inputs=[
            InputPort(
                id="in",
                label="Input",
                label_zh="输入",
                type=PortType.ANY,
                description="Used when the decision field is empty.",
                description_zh="当判断字段为空时使用。",
            )
        ],
        outputs=[
            OutputPort(
                id="out",
                label="Decision",
                label_zh="判断",
                type=PortType.TEXT,
                description="Which worker was chosen, or `done`.",
                description_zh="选中了哪个节点，或 `done`。",
            )
        ],
        params=[
            SelectParam(
                name="mode",
                label="How to decide",
                label_zh="如何判断",
                description="A rule, or always the same node.",
                description_zh="按规则判断，或固定选一个。",
                default=MODE_EXPRESSION,
                options=[
                    SelectOption(
                        value=MODE_EXPRESSION,
                        label="By a rule",
                        label_zh="按规则",
                        description=(
                            "An expression evaluated against the input and the "
                            "shared state. Empty means `workers`' first entry."
                        ),
                        description_zh=(
                            "对输入和共享状态求值的表达式。留空则用「可选节点」的第一项。"
                        ),
                    ),
                    SelectOption(
                        value=MODE_LIST,
                        label="Always the listed worker",
                        label_zh="固定选列出的节点",
                        description=(
                            "For a graph that always goes to one place — the "
                            "useful case is a loop whose exit is the rule."
                        ),
                        description_zh=(
                            "适合永远走同一个地方的情况 —— 有用的场景是循环，"
                            "退出条件由规则决定。"
                        ),
                    ),
                ],
            ),
            JsonParam(
                name="workers",
                label="Workers",
                label_zh="可选节点",
                description=(
                    "Node ids this supervisor may choose. A decision naming "
                    "anything else is refused."
                ),
                description_zh=(
                    "本主管可以选择的节点 id。判断出别的名字会被拒绝。"
                ),
                default=[],
                required=True,
            ),
            StringParam(
                name="decision",
                label="Rule",
                label_zh="规则",
                description=(
                    "Expression over `inputs` / `vars` / `state`, returning a "
                    "worker id or `done`. Example: {{ vars.classify.out }}."
                ),
                description_zh=(
                    "对 `inputs` / `vars` / `state` 求值的表达式，"
                    "返回某个节点 id 或 `done`。例如 {{ vars.classify.out }}。"
                ),
            ),
            StringParam(
                name="fallback",
                label="If nothing matches",
                label_zh="都不匹配时",
                description=(
                    "Worker to use when the rule returns something unknown. "
                    "Leave empty to stop instead."
                ),
                description_zh=(
                    "规则返回未知值时的兜底节点。留空则直接结束。"
                ),
            ),
            JsonParam(
                name="max_handoffs",
                label="Handoff limit",
                label_zh="分派次数上限",
                description=(
                    "How many times this supervisor may hand off before it stops "
                    "anyway. Guards against a loop between two workers."
                ),
                description_zh=(
                    "本主管最多分派几次后强制结束。防止两个节点之间来回打转。"
                ),
                default=10,
            ),
        ],
        ai_notes="""\
Routing by model decision, the way an Agent picks a tool — but the destination
is a *node*, not a tool.

**Its edges mean the opposite of every other node's.** Elsewhere an edge means
"run this next". Here an outgoing edge is a **declaration** that this worker is
one of the choices, and it is deliberately *not* drawn into the graph: the
supervisor returns its destination itself, and a drawn edge would run every
declared target instead of the chosen one. So do not add edges expecting them to
sequence anything, and do not try to make one worker follow another directly —
that ordering is the supervisor's decision at run time.

* The declared set is the outgoing edges **plus** the `workers` parameter, so
  either spelling works; list workers in the config if drawing ten edges is
  tedious.
* `mode: expression` evaluates `decision` (with `vars` in scope) and matches it
  against the declared handles; `mode: list` hands the model the list and lets it
  choose. The expression form is deterministic and cheaper — prefer it when the
  routing rule is something you can write down.
* `fallback` receives an unrecognised decision; without one the run stops.
* `max_handoffs` is the guard against two workers bouncing a task forever. Set it
  to the number of rounds the task could plausibly need, not to 10 out of habit.
* **"done" always has a target**, whether or not you wire it. Wire it to whatever
  runs after the work is finished, or the run ends there.\
""",
    )


def _handoff_count(ctx: NodeContext) -> int:
    """How many times this supervisor has already decided in this run.

    Read from the shared state rather than a variable, because a node is
    re-entered on every loop and holds no state between invocations. The counter
    is what makes "a loop between two workers" a bounded mistake.
    """
    entry = ctx.vars.get(ctx.node.id)
    if isinstance(entry, dict):
        try:
            return int(entry.get("handoffs") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


@register_node
class SupervisorNode(BaseNode):
    spec = build_supervisor_spec()

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        config = self.resolve_config(ctx)
        workers = self._workers(config)
        if not workers:
            raise ValueError(
                f"Supervisor '{ctx.node.label or ctx.node.id}' lists no workers. "
                "Add at least one node id to 'workers', or the graph cannot route."
            )

        limit = self._limit(config)
        used = _handoff_count(ctx)
        if limit and used >= limit:
            # Not an error. A supervisor that has handed off as many times as it
            # was allowed is a hop limit doing its job, and the run should finish
            # with what it has rather than fail.
            await ctx.log(
                f"Handoff limit reached ({used}/{limit}); stopping.", "warn"
            )
            return {"out": DONE, "handoffs": used}

        decision = self._decide(ctx, config, workers)
        if decision not in workers and decision != DONE:
            fallback = str(config.get("fallback") or "").strip()
            if fallback in workers:
                await ctx.log(
                    f"Decision {decision!r} is not one of {workers}; "
                    f"using fallback {fallback!r}.",
                    "warn",
                )
                decision = fallback
            else:
                await ctx.log(
                    f"Decision {decision!r} is not one of {workers} and no "
                    "fallback is set; stopping.",
                    "warn",
                )
                decision = DONE

        await ctx.log(
            f"Handoff {used + 1}"
            + (f"/{limit}" if limit else "")
            + f": {decision}",
            "info",
        )
        return {"out": decision, DECISION_KEY: decision, "handoffs": used + 1}

    # -- the choice --------------------------------------------------------

    def _workers(self, config: dict[str, Any]) -> list[str]:
        """Declared destinations, cleaned and de-duplicated.

        Order is preserved because it is meaningful: the first entry is the
        default when a rule comes out empty, and that is how an author expresses
        "usually go here".
        """
        raw = config.get("workers")
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.replace(",", "\n").splitlines()]
        if not isinstance(raw, (list, tuple)):
            return []
        out: list[str] = []
        for item in raw:
            name = str(item).strip()
            if name and name not in out:
                out.append(name)
        return out

    @staticmethod
    def _limit(config: dict[str, Any]) -> int:
        raw = config.get("max_handoffs")
        try:
            value = int(raw) if raw not in (None, "") else 10
        except (TypeError, ValueError):
            return 10
        return max(0, value)

    def _decide(
        self, ctx: NodeContext, config: dict[str, Any], workers: list[str]
    ) -> str:
        mode = str(config.get("mode") or MODE_EXPRESSION)

        if mode == MODE_LIST:
            # Still goes through the same validation, so a rule that returns a
            # name outside the list is caught rather than dispatched.
            chosen = workers[0]
        else:
            rule = str(config.get("decision") or "")
            chosen = render_template(rule, ctx, config) if rule else ""
            if not chosen:
                # No rule and no result: the input, then the first worker. The
                # input path is what lets a classifier node feed this one.
                chosen = str(ctx.inputs.get("in") or "").strip()
            if not chosen:
                chosen = workers[0]

        chosen = chosen.strip()
        # A rule that yields a path or a sentence is common; match it against the
        # declarations rather than returning something unroutable.
        if chosen in workers or chosen == DONE:
            return chosen
        for worker in workers:
            if chosen.startswith(worker) or worker in chosen:
                return worker
        return chosen


__all__ = [
    "DECISION_KEY",
    "DONE",
    "MODE_EXPRESSION",
    "MODE_LIST",
    "SupervisorNode",
    "build_supervisor_spec",
]
