"""Server-authored sentences in more than one language.

The AI modules produce a handful of strings themselves — "fixed every reported
problem", "the model did not change anything" — as opposed to the prose the model
writes. Those have to be translated, and the contract is the same one
`frontend/src/i18n/translations.ts` enforces: **a new message cannot ship in a
single language.**

The mechanism is small on purpose. Each module keeps its own table (the sentences
belong with the code that emits them) and calls :func:`render`, so there is one
implementation of "pick the language, fall back to English, substitute" rather
than one per module. `app/ai/repair.py` had the only copy; `gen_node.py` needs it
too, which is what moved it here.
"""

from __future__ import annotations

from typing import Any, Mapping

#: Which language a request asked for, normalised.
#: Kept loose (`str`) because callers pass whatever `normalize_language` returned
#: and an unrecognised value must fall back rather than raise.
Language = str


def render(
    table: Mapping[str, Mapping[str, str]],
    key: str,
    lang: Language,
    **values: Any,
) -> str:
    """One message from ``table`` in ``lang``, falling back to English.

    Raises ``KeyError`` for an unknown key rather than returning the key: a
    missing message is a programming error, and printing `resolved_warnings` at a
    user would hide it.
    """
    entry = table[key]
    text = entry.get(lang) or entry["en"]
    return text.format(**values) if values else text


def missing_translations(table: Mapping[str, Mapping[str, str]]) -> list[str]:
    """Keys with no Chinese text. Used by `scripts/check_i18n.py`.

    Exposed as a function rather than left to the checker so the rule lives beside
    the data it constrains, and so a test can assert it is empty.
    """
    return sorted(key for key, entry in table.items() if not entry.get("zh"))


__all__ = ["Language", "missing_translations", "render"]
