"""Every icon a node type names must exist in the frontend's whitelist.

This failed three times in one day — `brain`, `list-checks`, `git-fork` — each
time the same way: the node worked, the catalogue served it, and the canvas drew
a neutral placeholder because the name was not in `Icon.tsx`. A missing icon is
cosmetic, so nothing failed; it was only noticed by looking.

The check is cross-language on purpose. The names come from Python (a node's
`icon` field) and the whitelist lives in TypeScript, so neither side's tests can
see the other. Parsing the TSX with a regex is crude, and it is the right amount
of crude: the file's shape is one object literal, checked below.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.nodes.registry import list_node_type_specs

ICON_TSX = (
    pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "ui" / "Icon.tsx"
)


def _declared_icons() -> set[str]:
    """The keys of the `ICONS` record in `Icon.tsx`."""
    text = ICON_TSX.read_text(encoding="utf-8")
    start = text.index("const ICONS")
    end = text.index("};", start)
    body = text[start:end]
    return set(re.findall(r"^\s*'?([\w-]+)'?\s*:", body, re.M))


def _imported_icons() -> set[str]:
    """Names imported from `lucide-react`, i.e. the ones that can be referenced."""
    text = ICON_TSX.read_text(encoding="utf-8")
    start = text.index("from 'lucide-react'")
    # Walk back to the opening `import {`.
    head = text.rindex("import", 0, start)
    body = text[head:start]
    return set(re.findall(r"\b([A-Z]\w+)\b", body))


@pytest.mark.skipif(not ICON_TSX.exists(), reason="frontend not checked out here")
class TestNodeIconsAreRegistered:
    def test_the_whitelist_was_parsed(self):
        """If the file's shape changes, every other assertion here passes
        vacuously. This one fails instead."""
        icons = _declared_icons()
        assert len(icons) > 10, f"only parsed {len(icons)} icons — did the file change shape?"

    def test_every_node_icon_is_declared(self):
        declared = _declared_icons()
        missing = sorted(
            {spec.icon for spec in list_node_type_specs() if spec.icon} - declared
        )
        assert not missing, (
            "these node icons are not in the frontend whitelist, so the canvas "
            f"draws a placeholder instead: {missing}. Add them to "
            f"{ICON_TSX.name} (lucide has them; the list is explicit to keep the "
            "bundle small)."
        )

    def test_every_declared_icon_is_actually_imported(self):
        """A name in the record with no import is a `ReferenceError` at load, not
        a fallback — the whole canvas fails, which is worse than a placeholder."""
        declared = _declared_icons()
        imported = _imported_icons()
        # Keys are kebab-case lucide names; imports are PascalCase identifiers.
        expected_imports = {
            "".join(part.capitalize() for part in name.split("-"))
            for name in declared
        }
        missing = sorted(expected_imports - imported)
        assert not missing, f"declared in ICONS but not imported: {missing}"
