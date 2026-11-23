"""Sub-flow node — one saved workflow used as a single step in another.

This node type is unusual: it never executes as written. The compiler expands it
into the referenced workflow's contents before building the LangGraph graph (see
``app.graph.subgraphs``), and this class is what remains of it — a pass-through
that hands its inputs on to the sub-flow's first node.

That pass-through is the whole reason the node stays. Inputs here are resolved
from ``vars[<source node id>]``, so a node with the parent's wiring on its input
side is indistinguishable from that sub-flow's own Start node — which is what lets
a piece of workflow be reused with no adapter needed.

Config
------
``ref`` is the referenced workflow's id. ``ref_hash`` is the digest of that
definition at the moment this parent was last saved; the compiler refuses to run
when they disagree, so editing a sub-workflow reports an error instead of quietly
changing what every graph using it does.

The dropdown is rebuilt from the workflow store (see the refresher at the bottom)
for the same reason the Tool node's is: the list is deployment data, and a spec
frozen at import would offer workflows that have since been deleted.
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
)
from app.graph.subgraphs import list_subgraph_options
from app.nodes.base import BaseNode, NodeContext
from app.nodes.registry import register_node, register_spec_refresher


def subgraph_options() -> list[SelectOption]:
    """Saved workflows, as dropdown entries."""
    return [
        SelectOption(
            label=str(item.get("label") or item.get("value") or ""),
            value=str(item.get("value") or ""),
            description=str(item.get("description") or ""),
        )
        for item in list_subgraph_options()
    ]


def build_subgraph_spec() -> NodeTypeSpec:
    """Build the spec from the current workflow store. Also the refresher."""
    options = subgraph_options()
    return NodeTypeSpec(
        type="subgraph",
        label="Sub-flow",
        label_zh="子流程",
        category=NodeCategory.LOGIC,
        description=(
            "Run another saved workflow as one step. Its nodes appear in the "
            "timeline under this node's name."
        ),
        description_zh=(
            "把另一个已保存的工作流当作一步来跑。它的节点会以本节点的名字为前缀"
            "出现在运行时间线里。"
        ),
        icon="boxes",
        color="#8b5cf6",
        inputs=[
            InputPort(
                id="in",
                label="Input",
                label_zh="输入",
                type=PortType.ANY,
                description="Passed to the sub-flow's first node.",
                description_zh="传给子流程的第一个节点。",
            )
        ],
        outputs=[
            OutputPort(
                id="out",
                label="Result",
                label_zh="结果",
                type=PortType.ANY,
                description="Whatever the sub-flow's last node produced.",
                description_zh="子流程最后一个节点的产出。",
            )
        ],
        params=[
            SelectParam(
                name="ref",
                label="Workflow",
                label_zh="工作流",
                description="The saved workflow to run here.",
                description_zh="在这里运行哪个已保存的工作流。",
                required=True,
                default=options[0].value if options else "",
                options=options,
            ),
            # Recorded automatically on save. Shown in the Inspector because
            # "which version am I pinned to" is a question a user asks out loud
            # when the compiler tells them the sub-flow changed.
            JsonParam(
                name="note",
                label="Note",
                label_zh="备注",
                description="Optional reminder for whoever reads the canvas.",
                description_zh="给看画布的人留的可选提醒。",
                default="",
                advanced=True,
            ),
        ],
    )


@register_node
class SubgraphNode(BaseNode):
    spec = build_subgraph_spec()

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        """Pass the input straight through.

        This runs only if expansion did not happen — for example a node-level
        test that invokes one node in isolation. In a compiled graph this method
        is never reached: the node's job is done by the edges around it.
        """
        value = ctx.input("in")
        await ctx.log("Sub-flow entry passes its input to the first inner node.", "info")
        return {"out": value}


register_spec_refresher("subgraph", build_subgraph_spec)

#: The pinned digest lives in config but is not a user-editable parameter, so it
#: is written directly by the save path rather than declared here. Declaring it
#: would put a hash in the Inspector, which helps nobody.
REF_HASH_CONFIG_KEY = "ref_hash"

__all__ = ["REF_HASH_CONFIG_KEY", "SubgraphNode", "build_subgraph_spec", "subgraph_options"]
