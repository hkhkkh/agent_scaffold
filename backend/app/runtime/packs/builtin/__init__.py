"""The capability packs this runtime ships with.

Listed explicitly rather than discovered: the exporter writes this set into an
agent's `manifest.yml`, so "what shipped" and "what was declared" have to be the
same list. A test walks this directory and fails if a module is not listed here.
"""

from app.runtime.packs.builtin.core import CORE_PACK
from app.runtime.packs.builtin.files import FILES_PACK

__all__ = ["BUILTIN_PACKS", "CORE_PACK", "FILES_PACK"]

BUILTIN_PACKS = (CORE_PACK, FILES_PACK)
