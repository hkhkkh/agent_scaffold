"""Check that catalogue localisation resolves correctly.

Run with the project's interpreter from the `backend` directory:
    python -m scripts.check_i18n
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

from app.api.catalogue import get_node_types, get_state_fields
from app.graph.schema import localize, normalize_language
from app.graph.validate import validate_graph
from app.graph.schema import WorkflowGraph


def main() -> int:
    failures: list[str] = []
    lines: list[str] = []

    # --- language tag normalisation ---------------------------------------
    cases = {
        "en": "en",
        "zh": "zh",
        "zh-CN": "zh",
        "zh-Hans": "zh",
        "zh_TW": "zh",
        "EN-US": "en",
        "fr": "en",  # unsupported falls back rather than raising
        "": "en",
        None: "en",
    }
    for raw, expected in cases.items():
        got = normalize_language(raw)
        if got != expected:
            failures.append(f"normalize_language({raw!r}) = {got!r}, expected {expected!r}")
    lines.append(f"normalize_language: {len(cases)} cases checked")

    # --- the _zh fields must never reach the client ------------------------
    en = get_node_types("en")
    zh = get_node_types("zh")

    leaked = [spec["type"] for spec in zh["types"] if any(k.endswith("_zh") for k in spec)]
    if leaked:
        failures.append(f"_zh fields leaked to the client for: {leaked}")

    if en["count"] != zh["count"]:
        failures.append(f"count differs between languages: {en['count']} vs {zh['count']}")

    # --- switching language must not change identifiers -------------------
    # A node/param/port id is data, not text: a workflow saved in one language
    # must load in the other. This is the check that would catch someone
    # "localising" an id by accident.
    for en_spec, zh_spec in zip(en["types"], zh["types"]):
        if en_spec["type"] != zh_spec["type"]:
            failures.append(f"type id changed: {en_spec['type']} -> {zh_spec['type']}")
        en_ports = [p["id"] for p in en_spec["inputs"] + en_spec["outputs"]]
        zh_ports = [p["id"] for p in zh_spec["inputs"] + zh_spec["outputs"]]
        if en_ports != zh_ports:
            failures.append(f"port ids changed for {en_spec['type']}")

        en_params = [p["name"] for p in en_spec["params"]]
        zh_params = [p["name"] for p in zh_spec["params"]]
        if en_params != zh_params:
            failures.append(f"param names changed for {en_spec['type']}")
    lines.append(f"identifier stability: {len(en['types'])} node types checked")

    # --- state field options ----------------------------------------------
    zh_fields = get_state_fields("zh")
    overwrite = next(r for r in zh_fields["reducers"] if r["value"] == "overwrite")
    if overwrite["label"] != "覆盖":
        failures.append(f"reducer label not localised: {overwrite['label']!r}")
    if "label_zh" in overwrite:
        failures.append("reducer still carries label_zh")

    en_fields = get_state_fields("en")
    overwrite_en = next(r for r in en_fields["reducers"] if r["value"] == "overwrite")
    if overwrite_en["label"] != "Overwrite":
        failures.append(f"English reducer label wrong: {overwrite_en['label']!r}")

    # --- fallback: a translation gap must not blank a label ---------------
    # This is the property that lets node types be translated incrementally.
    synthetic = {
        "label": "Retriever",
        "description": "Rank documents.",
        "label_zh": "",  # not translated yet
        "description_zh": "按关键词排序文档。",
    }
    resolved = localize(synthetic, "zh")
    if resolved["label"] != "Retriever":
        failures.append(f"empty override should fall back to English, got {resolved['label']!r}")
    if resolved["description"] != "按关键词排序文档。":
        failures.append("non-empty override was not applied")

    # --- validation messages ------------------------------------------------
    # Two properties matter. First, statically: *every* rule must carry a
    # Chinese sentence, including rules a sample graph would never trigger. The
    # AST walk is what actually guarantees that — a new rule added without a
    # translation fails here rather than silently shipping English into the
    # Chinese UI.
    validate_src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "graph" / "validate.py"
    ).read_text(encoding="utf-8")
    validate_tree = ast.parse(validate_src)

    def has_han(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in text)

    def zh_literal(node: ast.expr) -> bool:
        """A str literal, or an f-string whose literal parts contain Han."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return has_han(node.value)
        if isinstance(node, ast.JoinedStr):
            return any(
                isinstance(part, ast.Constant)
                and isinstance(part.value, str)
                and has_han(part.value)
                for part in node.values
            )
        return False

    issue_calls = [
        node
        for node in ast.walk(validate_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("error", "warn")
    ]
    untranslated = [
        (call.lineno, ast.unparse(call.args[0]))
        for call in issue_calls
        if not any(zh_literal(arg) for arg in call.args)
    ]
    for lineno, code in untranslated:
        failures.append(f"validate.py:{lineno} rule '{code}' has no Chinese message")
    lines.append(
        f"validation messages: {len(issue_calls) - len(untranslated)}/{len(issue_calls)}"
        f" rules translated"
    )

    # Second, dynamically: the report must actually switch language and must
    # not ship the *_zh carrier fields to the client.
    broken = WorkflowGraph.model_validate(
        {
            "id": "i18n-probe",
            "name": "probe",
            "nodes": [
                # No Start node, so ``missing_entry`` fires; the lone LLM node is
                # also orphaned and missing its required prompt. The label is
                # deliberately not a catalogue name, so the English-label check
                # below only ever flags genuinely untranslated interpolations.
                {
                    "id": "n1",
                    "type": "llm",
                    "label": "Probe Node",
                    "position": {"x": 0, "y": 0},
                    "config": {"prompt": ""},
                }
            ],
            "edges": [],
        }
    )
    report = validate_graph(broken)
    en_issues = localize(report.model_dump(mode="json"), "en")["issues"]
    zh_issues = localize(report.model_dump(mode="json"), "zh")["issues"]

    if not en_issues:
        failures.append("probe graph produced no issues, so the check proves nothing")
    else:
        codes = [i["code"] for i in en_issues]
        if "missing_entry" not in codes:
            failures.append(f"probe graph did not trigger missing_entry, got {codes}")
        if any(i["message"] == j["message"] for i, j in zip(en_issues, zh_issues)):
            failures.append("at least one issue message is identical in en and zh")
        if any(
            not any("\u4e00" <= ch <= "\u9fff" for ch in issue["message"])
            for issue in zh_issues
        ):
            failures.append("a Chinese validation message contains no Han characters")
        if any(k.endswith("_zh") for i in zh_issues for k in i):
            failures.append("validation issue still carries message_zh")
        if any("\u4e00" <= ch <= "\u9fff" for i in en_issues for ch in i["message"]):
            failures.append("English validation message contains Han characters")

        # A Chinese sentence must not quote an English *catalogue* name: the
        # message names a control the user is looking at, and that control is
        # labelled in Chinese. User-authored labels are exempt — they are the
        # user's own words — which is why only quoted 「...」 spans are inspected.
        english_labels = {
            label
            for spec in en["types"]
            for label in (
                [p["label"] for p in spec["params"]]
                + [p["label"] for p in spec["inputs"] + spec["outputs"]]
            )
        }
        quoted = {
            span
            for issue in zh_issues
            for span in re.findall("\u300c([^\u300d]+)\u300d", issue["message"])
        }
        for span in sorted(quoted & english_labels):
            failures.append(f"Chinese validation message quotes the English label {span!r}")

        lines.append(
            f"validation localisation: {len(en_issues)} issues, "
            f"zh messages differ from en"
        )

    # --- how much of the catalogue is actually translated ------------------
    translated = sum(1 for s in zh["types"] if s["description"] != en["types"][zh["types"].index(s)]["description"])
    lines.append(f"translated node types: {translated}/{zh['count']}")

    # --- security and egress messages --------------------------------------
    # These are read from the modules rather than from a sample request, for the
    # same reason the validation messages are AST-walked: whether a message is
    # reachable from a test says nothing about whether it is translated, and the
    # ones hardest to reach are exactly the ones most likely to be missed.
    from app.runtime.ai.messages import missing_translations
    from app.runtime.egress import _DENY_MESSAGES, _HOST_REASONS
    from app.security import deps as security_deps
    from app.security import policy as security_policy

    message_tables = {
        "auth": security_deps._MESSAGES,
        "egress": _DENY_MESSAGES,
        "public": security_policy._MESSAGES,
    }
    total_messages = 0
    for name, table in message_tables.items():
        total_messages += len(table)
        untranslated = missing_translations(table)
        if untranslated:
            failures.append(f"{name}: messages with no Chinese: {untranslated}")
        for key, entry in table.items():
            en_slots = set(re.findall(r"\{(\w+)\}", entry["en"]))
            zh_slots = set(re.findall(r"\{(\w+)\}", entry["zh"]))
            if en_slots != zh_slots:
                failures.append(f"{name}.{key}: placeholders differ {en_slots} vs {zh_slots}")
            if not any("\u4e00" <= ch <= "\u9fff" for ch in entry["zh"]):
                failures.append(f"{name}.{key}: Chinese text has no Han characters")
            if any("\u4e00" <= ch <= "\u9fff" for ch in entry["en"]):
                failures.append(f"{name}.{key}: English text contains Han characters")

    missing_hosts = sorted(k for k, v in _HOST_REASONS.items() if not v)
    if missing_hosts:
        failures.append(f"egress host fragments with no translation: {missing_hosts}")
    unreachable = sorted(k for k in _HOST_REASONS if not k)
    if unreachable:
        failures.append(f"egress host fragment keys must be the English text: {unreachable}")
    lines.append(
        f"security messages: {total_messages} in {len(message_tables)} tables, "
        f"{len(_HOST_REASONS)} host fragments"
    )

    report = "\n".join(lines)
    if failures:
        report += "\n\nFAILURES:\n" + "\n".join(f"  - {f}" for f in failures)

    # Written to a file as well as stdout: the console here mangles UTF-8, and
    # the Chinese strings are the point of the check.
    with open("i18n_report.txt", "w", encoding="utf-8") as handle:
        handle.write(report + "\n")

    print(f"RESULT={'FAIL' if failures else 'PASS'} ({len(failures)} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
