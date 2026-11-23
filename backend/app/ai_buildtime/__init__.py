"""Build-time AI: authoring a graph and filling in a node from a description.

**Platform (layer 1), not runtime.** These are the canvas's authoring abilities —
they call a model to *write* a definition, and an exported agent never does that:
it is handed a definition and runs it.

The two halves used to share one ``app/ai/`` package, which is why the split can
look arbitrary from outside: ``runtime/ai/gateway.py`` (calling a model *during* a
run) belongs to the runtime, while ``gen_graph.py`` (calling a model to *author* a
graph) does not. The boundary is **when** the model is called, not which library
is used.

Nothing here ships in an agent package — ``runtime/export.py``'s ``RUNTIME_PATHS``
lists what does, and none of these five modules is on it.
"""

__all__: list[str] = []
