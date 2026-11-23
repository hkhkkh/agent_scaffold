"""Redaction: by key name, and inside strings.

The second half is new, and it closes a gap that was documented as a known
limitation rather than a bug: `redact` matched **key names**, so a credential
templated into a prompt string was not under a secret-looking key and survived
into step payloads and the transcript.

What is worth defending here is not "the regex matches" but three properties:

* **The gap is actually closed end to end** — a real workflow that templates a
  key into its output no longer carries the key into persistence. This is the
  assertion the reverse-validation probe breaks, so it has to be able to fail.
* **Ordinary data is untouched.** A redactor that mangles UUIDs, hashes, or
  ordinary prose is one an operator switches off, and then nothing is redacted
  at all. Every pattern is vendor-prefixed for this reason, so the tests pin the
  negative cases as hard as the positive ones.
* **The rewrite is delegated, not reimplemented.** `apply_strategy` is upstream's;
  asserting on its exact output format is what would catch a swap to a
  home-grown replacement with different semantics.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.graph.schema import RunCreateRequest, RunStatus, WorkflowGraph
from app.runtime.manager import RunManager
from app.runtime.runs import NullRunStore, set_run_store
from app.runtime.serialize import (
    CONTENT_SECRET_PATTERNS,
    MASK,
    content_redaction_enabled,
    find_content_secrets,
    looks_secret,
    redact,
    redact_content,
)
from app.settings import Settings


class _RecordingStore(NullRunStore):
    """A null store that keeps what it was handed, so a test can inspect it."""

    def __init__(self) -> None:
        self.finishes: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    def finish(self, run: dict[str, Any], steps: list[dict[str, Any]]) -> None:
        self.finishes.append((run, steps))


@pytest.fixture(autouse=True)
def _restore_store():
    """The store is process-wide, so every test here must put it back."""
    yield
    set_run_store(None)

#: Values with the shapes the patterns are meant to catch. Prefixed on purpose —
#: each one is unambiguous, which is why false positives are near zero.
SECRETS = [
    ("sk-abcdefghijklmnopqrstuvwxyz012345", "api_key"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "api_key"),
    ("xoxb-1234567890-abcdefghijkl", "api_key"),
    ("AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
    (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "jwt",
    ),
]

#: Ordinary values that must come back unchanged.
NOT_SECRETS = [
    "a plain sentence with no credentials in it",
    "550e8400-e29b-41d4-a716-446655440000",
    "sha256:e3b0c44298fc1c149afbf4c8996fb924",
    "https://api.example.com/v1/chat/completions",
    "user@example.com",
    "the quick brown fox jumps over the lazy dog",
]


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# 1. The engine we kept: key-name matching is unchanged
# ---------------------------------------------------------------------------


class TestKeyNameRedactionStillWorks:
    """The original behaviour, so that adding a second pass cannot quietly replace it."""

    def test_a_secret_keyed_value_is_masked(self):
        assert redact({"api_key": "anything"}) == {"api_key": MASK}

    def test_nesting_is_walked(self):
        result = redact({"outer": {"inner": {"password": "hunter2"}}})
        assert result == {"outer": {"inner": {"password": MASK}}}

    def test_the_not_secret_allowlist_still_wins(self):
        """`auth_mode: "oauth"` must survive, or the value becomes useless."""
        assert redact({"auth_mode": "oauth"}) == {"auth_mode": "oauth"}
        assert looks_secret("auth_mode") is False

    def test_an_empty_value_is_left_alone(self):
        """Masking an absent credential invents a value where there was none."""
        assert redact({"api_key": ""}) == {"api_key": ""}
        assert redact({"api_key": None}) == {"api_key": None}


# ---------------------------------------------------------------------------
# 2. The new pass: content scanning
# ---------------------------------------------------------------------------


class TestContentScanning:
    @pytest.mark.parametrize(("secret", "kind"), SECRETS)
    def test_a_prefixed_secret_in_a_string_is_masked(self, secret, kind):
        text = f"the value is {secret} and that is all"
        out = redact_content(text)
        assert secret not in out
        assert f"[REDACTED_{kind.upper()}]" in out, (
            "the output format is upstream's apply_strategy; a different format "
            "means the rewrite was reimplemented rather than delegated"
        )
        assert out.startswith("the value is ")
        assert out.endswith(" and that is all")

    @pytest.mark.parametrize("text", NOT_SECRETS)
    def test_ordinary_strings_are_untouched(self, text):
        """The half that keeps the feature usable.

        A redactor with false positives on UUIDs or prose is one an operator
        disables, and then the real credentials are unprotected too.
        """
        assert redact_content(text) == text

    def test_no_match_means_no_allocation(self):
        assert find_content_secrets("nothing here") == []

    def test_a_very_long_string_is_skipped(self):
        """Bounded on purpose: a regex over every megabyte of model output is a cost."""
        from app.runtime.serialize import MAX_SCANNED_STRING

        filler = "x" * (MAX_SCANNED_STRING + 1)
        text = f"{filler} sk-abcdefghijklmnopqrstuvwxyz012345"
        assert redact_content(text) == text

    def test_several_secrets_in_one_string_are_all_masked(self):
        text = "a sk-abcdefghijklmnopqrstuvwxyz012345 b AKIAIOSFODNN7EXAMPLE c"
        out = redact_content(text)
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_the_engine_is_upstreams(self):
        """If upstream's formatter changes, this test should notice.

        Not a tautology: it pins that we call their function rather than a local
        one that happens to produce similar-looking text today.
        """
        from langchain.agents.middleware.pii import apply_strategy

        text = "token sk-abcdefghijklmnopqrstuvwxyz012345"
        ours = redact_content(text)
        theirs = apply_strategy(text, find_content_secrets(text), "redact")
        assert ours == theirs


# ---------------------------------------------------------------------------
# 3. Wiring: through `redact`, and switchable
# ---------------------------------------------------------------------------


class TestThePassIsWiredIntoRedact:
    def test_a_secret_in_a_nested_string_is_masked(self):
        payload = {"node": {"output": "here it is sk-abcdefghijklmnopqrstuvwxyz012345"}}
        out = redact(payload)
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in str(out)

    def test_a_secret_in_a_list_is_masked(self):
        out = redact(["ok", "sk-abcdefghijklmnopqrstuvwxyz012345"])
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in str(out)

    def test_a_caller_can_override_for_one_call(self):
        text = "sk-abcdefghijklmnopqrstuvwxyz012345"
        assert "sk-" not in redact(text, content=True)
        assert redact(text, content=False) == text

    def test_the_setting_turns_it_off(self, monkeypatch):
        from app.runtime import serialize

        monkeypatch.setattr(serialize, "content_redaction_enabled", lambda: False)
        assert redact("sk-abcdefghijklmnopqrstuvwxyz012345") == (
            "sk-abcdefghijklmnopqrstuvwxyz012345"
        )

    def test_it_defaults_to_on(self):
        """A redactor that is off by default is one nobody notices is off."""
        assert content_redaction_enabled() is True

    def test_the_pattern_list_is_not_empty(self):
        """Guard against a refactor that empties the rules and silently stops checking."""
        assert len(CONTENT_SECRET_PATTERNS) >= 4


# ---------------------------------------------------------------------------
# 4. The gap, end to end — the assertion the probe breaks
# ---------------------------------------------------------------------------


def _templating_graph(workflow_id: str = "wf-redact-e2e") -> WorkflowGraph:
    """A graph whose prompt node templates the credential into its output.

    That is the realistic path: a node reads an upstream value and puts it in a
    string, so the credential ends up in `vars[node]["out"]` — a place no
    key-name rule can see.
    """

    def node(node_id: str, node_type: str, **config: Any) -> dict[str, Any]:
        return {"id": node_id, "type": node_type, "label": node_id, "config": config}

    def edge(source: str, target: str) -> dict[str, Any]:
        return {
            "id": f"{source}->{target}",
            "source": source,
            "sourceHandle": "out",
            "target": target,
            "targetHandle": "in",
        }

    return WorkflowGraph(
        id=workflow_id,
        nodes=[
            node("start", "start", input_schema={}),
            node(
                "p0",
                "prompt",
                template="the key is {{ vars.start.out.api_key }}",
            ),
            node("end", "end", output_label="result"),
        ],
        edges=[edge("start", "p0"), edge("p0", "end")],
    )


class TestTheDocumentedGapIsClosed:
    async def test_a_templated_credential_does_not_reach_a_step_payload(self):
        """The end-to-end version of the limitation this change removes.

        Asserted on what **reaches the store**, not on `context.steps`. The
        in-memory records are the live process's own data and are deliberately
        raw; redaction happens when a payload leaves, in `_persist_finish`, in
        `_tracked_step`, and in the transcript. Asserting on the raw record would
        have been a decorative test — it fails whether or not redaction works.
        """
        store = _RecordingStore()
        set_run_store(store)

        manager = RunManager()
        graph = _templating_graph()
        context = await manager.create(
            graph.id,
            graph,
            RunCreateRequest(
                workflow_id=graph.id,
                inputs={"api_key": "sk-abcdefghijklmnopqrstuvwxyz012345"},
                recursion_limit=200,
            ),
        )
        await manager.start(context)
        assert context.task is not None
        await context.task

        assert context.status is RunStatus.SUCCEEDED
        assert store.finishes, "nothing was handed to the store"

        _, steps = store.finishes[0]
        assert steps, "the run recorded no steps, so this proves nothing"
        dumped = repr(steps)
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in dumped, (
            "a credential templated into a node output reached the store"
        )

        # And the prompt node really did mirror it, so this is about redaction
        # rather than about the credential never travelling.
        assert any("the key is" in repr(step.get("outputs")) for step in steps)

    async def test_the_transcript_is_clean_too(self):
        """The transcript is the second door, and it was the leaking one.

        It previously wrote step inputs and outputs verbatim while the database
        received redacted copies — so a credential was absent from SQLite and
        present in the JSON file beside it. Kept as a test because the two
        artefacts are written by different methods and nothing else compares them.
        """
        import json

        manager = RunManager()
        graph = _templating_graph("wf-redact-transcript")
        context = await manager.create(
            graph.id,
            graph,
            RunCreateRequest(
                workflow_id=graph.id,
                inputs={"api_key": "sk-abcdefghijklmnopqrstuvwxyz012345"},
                recursion_limit=200,
            ),
        )
        await manager.start(context)
        assert context.task is not None
        await context.task

        assert context.transcript_path, "no transcript was written"
        body = json.loads(open(context.transcript_path, encoding="utf-8").read())
        text = json.dumps(body)
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in text, (
            "the transcript leaked a credential that the run row did not"
        )
        # The transcript is still a usable record: the steps and events are there.
        assert body["steps"], body.keys()
        assert body["events"], body.keys()
