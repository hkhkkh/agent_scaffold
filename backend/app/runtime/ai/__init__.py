"""The runtime's model-calling half of what used to be ``app/ai/``.

``gateway.py`` (the capability seam the LLM and Agent nodes call) and
``provider.py`` (how a chat model is built) are runtime: a node calls a model
*during* a run, and an exported agent runs nodes.

The other half of that package — ``gen_graph`` / ``gen_node`` / ``repair`` /
``prompts`` / ``parsing`` — stayed on the platform as ``app.ai_buildtime``. Those
call a model to *author* a definition, which is what the canvas does and what an
exported agent never does.
"""

from app.runtime.ai.provider import build_chat_model, resolve_llm_config

__all__ = ["build_chat_model", "resolve_llm_config"]
