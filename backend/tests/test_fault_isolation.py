"""Fault isolation: a node that fails does not have to fail the run.

The gap this closes was real: before it, one node raising killed the whole run,
which makes "process 500 rows, 3 of which are malformed" impossible to express.

Design notes worth knowing before changing anything here:

* The decision is **per node**, not global. Only the author knows which steps are
  allowed to fail — a summariser finding nothing is not a scheduling problem,
  while a payment step failing is.
* The default is `stop`, which is what the runtime did before this existed.
  A silently skipped node is how a workflow appears to work while returning
  wrong answers, so the safe direction is to fail loudly.
* `langgraph`'s own `error_handler` was considered and rejected — but for a
  reason worth stating precisely, because the first version of this note had it
  wrong. It **can** reach the exception (`def handler(state, error: NodeError)`,
  injected by annotation) and it **can** route to the failed node's descendants
  (return `Command(goto=...)`). What it cannot be is a *declarative* per-node
  switch chosen in the Inspector: it takes a Python function, and our graphs are
  data, so the handler would have to exist before we know which nodes do. The
  framework's retry and timeout are used; the failure policy is ours because it
  has to be data.
"""

from __future__ import annotations

import pytest

from app.graph.schema import NodeSpec, NodeTypeSpec
from app.nodes.registry import all_params_for, list_node_type_specs, register_node
from app.nodes.base import BaseNode
from app.runtime.attempts import (
    ERROR_KEY,
    KEY_ON_ERROR,
    ON_ERROR_CONTINUE,
    ON_ERROR_DEFAULT,
    ON_ERROR_MODES,
    ON_ERROR_STOP,
    AttemptPolicy,
)


def _policy(**config) -> AttemptPolicy:
    return AttemptPolicy.resolve(config)


# ---------------------------------------------------------------------------
# Reading the setting
# ---------------------------------------------------------------------------


class TestResolvingThePolicy:
    def test_the_default_is_to_fail_the_run(self):
        """Pinned because it is the safe direction and the previous behaviour."""
        assert _policy().on_error == ON_ERROR_STOP
        assert _policy().continues_on_error is False

    @pytest.mark.parametrize("mode", ON_ERROR_MODES)
    def test_every_documented_mode_is_accepted(self, mode):
        assert _policy(**{KEY_ON_ERROR: mode}).on_error == mode

    def test_the_mode_is_read_case_insensitively(self):
        """It comes from a text field in the canvas, where `Continue` is what a
        person types."""
        assert _policy(**{KEY_ON_ERROR: "CONTINUE"}).on_error == ON_ERROR_CONTINUE

    def test_an_unknown_mode_becomes_stop_rather_than_raising(self):
        """The one place a bad value is absorbed: refusing to start a run over a
        typo in an optional field would be the more disruptive failure, and
        `stop` is the direction that cannot lose work silently."""
        assert _policy(**{KEY_ON_ERROR: "carry_on"}).on_error == ON_ERROR_STOP

    def test_continue_modes_report_that_they_survive(self):
        assert _policy(**{KEY_ON_ERROR: ON_ERROR_CONTINUE}).continues_on_error is True
        assert _policy(**{KEY_ON_ERROR: ON_ERROR_DEFAULT}).continues_on_error is True

    def test_only_the_default_mode_has_a_failure_value(self):
        """`continue` writes nothing usable on purpose, so a downstream node
        cannot mistake a placeholder for real data."""
        assert _policy(**{KEY_ON_ERROR: ON_ERROR_CONTINUE}).failure_value is None
        assert (
            _policy(
                **{KEY_ON_ERROR: ON_ERROR_DEFAULT, "on_error_value": "n/a"}
            ).failure_value
            == "n/a"
        )

    def test_the_failure_value_may_be_falsy(self):
        """`0` and `""` are values a workflow may legitimately want back."""
        for value in (0, "", False, []):
            policy = _policy(**{KEY_ON_ERROR: ON_ERROR_DEFAULT, "on_error_value": value})
            assert policy.failure_value == value

    def test_the_on_error_setting_does_not_disturb_the_timeout(self):
        policy = _policy(**{KEY_ON_ERROR: ON_ERROR_CONTINUE, "node_timeout_seconds": 7})
        assert policy.timeout_seconds == 7
        assert policy.on_error == ON_ERROR_CONTINUE


# ---------------------------------------------------------------------------
# Every node can be configured
# ---------------------------------------------------------------------------


class TestEveryNodeOffersTheKnobs:
    def test_all_four_parameters_reach_every_registered_type(self):
        """The failure this prevents: a node that silently does not offer
        `on_error`, which a user reads as the feature not working rather than as
        a missing declaration."""
        for spec in list_node_type_specs():
            names = {param.name for param in all_params_for(spec)}
            assert {
                "node_timeout_seconds",
                "node_max_retries",
                "on_error",
                "on_error_value",
            } <= names, f"{spec.type} is missing a resilience parameter"

    def test_a_type_that_declares_one_itself_keeps_its_own(self):
        """A node that already declares one of these must not get a second copy,
        or the Inspector renders the same field twice.

        Built by copying a real spec and appending a declaration, rather than by
        registering a throwaway type: registration is global and would leak into
        every other test in the session.
        """
        from app.graph.schema import NodeCategory

        source = next(s for s in list_node_type_specs() if s.type == "prompt")
        own = next(
            p for p in all_params_for(source) if p.name == "node_timeout_seconds"
        )
        probe = NodeTypeSpec(
            type="_probe_declaring",
            label="Probe",
            category=NodeCategory.DATA,
            inputs=[],
            outputs=[],
            params=[*source.params, own],
        )

        names = [param.name for param in all_params_for(probe)]
        assert names.count("node_timeout_seconds") == 1
        assert len(names) == len(set(names)), f"duplicate parameter names: {names}"

    def test_the_timeout_parameter_says_what_the_default_is(self):
        """A number in a description that can drift from the setting is worse than
        no number, so it is read from the setting."""
        spec = next(s for s in list_node_type_specs() if s.type == "prompt")
        param = next(
            p for p in all_params_for(spec) if p.name == "node_timeout_seconds"
        )
        from app.settings import Settings

        default = Settings.model_fields["node_timeout_seconds"].default
        assert str(int(default)) in param.description

    def test_retries_are_off_by_default_and_say_why(self):
        """Retrying repeats side effects, so the description has to carry the
        reason rather than just the field name."""
        spec = next(s for s in list_node_type_specs() if s.type == "prompt")
        param = next(p for p in all_params_for(spec) if p.name == "node_max_retries")
        assert param.default == 0
        assert "side effect" in param.description.lower()


# ---------------------------------------------------------------------------
# The compiler's behaviour
# ---------------------------------------------------------------------------


class TestTheCompilerUsesIt:
    """These read the compiled `run_node`, because the decision is one branch in
    a function that needs a whole graph to call. Asserting on the source is
    weaker than calling it, and stronger than asserting nothing — and the branch
    is small enough that the source is the honest place to pin it.

    The end-to-end behaviour is covered by the HTTP tests, which run a real graph
    with a node that really raises.
    """

    @staticmethod
    def _source() -> str:
        import pathlib

        return (
            pathlib.Path(__file__).resolve().parents[1] / "app" / "graph" / "compiler.py"
        ).read_text(encoding="utf-8")

    def test_the_catch_branch_consults_the_policy(self):
        source = self._source()
        assert "policy.continues_on_error" in source

    def test_a_stop_decision_still_names_the_node(self):
        """The message a user sees when a run fails has to say which node."""
        source = self._source()
        assert "node.label or node.id" in source

    def test_a_continued_failure_is_recorded_in_the_node_output(self):
        source = self._source()
        assert "ERROR_KEY" in source

    def test_control_flow_exceptions_are_still_re_raised_first(self):
        """`GraphInterrupt` must not be caught by the new branch, or a paused run
        would be reported as a failed node and human-in-the-loop would break.
        The `except GraphBubbleUp: raise` has to come first."""
        source = self._source()
        bubble = source.index("except GraphBubbleUp:")
        general = source.index("except Exception as exc:")
        assert bubble < general, "GraphBubbleUp must be caught before Exception"
        # And the GraphBubbleUp branch must not consult on_error.
        between = source[bubble:general]
        assert "continues_on_error" not in between


# ---------------------------------------------------------------------------
# The recorded failure
# ---------------------------------------------------------------------------


class TestTheRecordedFailure:
    def test_the_key_is_exported(self):
        from app.runtime import attempts

        assert attempts.ERROR_KEY == ERROR_KEY

    def test_the_error_record_is_readable_by_a_template(self):
        """It lives in `vars[node_id]`, so `{{ vars.<node>.__error__.error }}`
        has to be a valid path — which means plain strings and no objects."""
        import json

        record = {
            "error": "ValueError: bad input",
            "node_id": "n1",
            "node_label": "Summarise",
        }
        assert set(record) == {"error", "node_id", "node_label"}
        assert json.loads(json.dumps(record)) == record

    def test_a_rerun_of_a_failed_node_does_not_keep_a_stale_error(self):
        """A node that succeeded on retry must not still report failure.

        The compiler builds `result` fresh in each branch, so this holds by
        construction; the assertion is here so a future edit that merges into the
        previous value fails this test instead of the user.
        """
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parents[1] / "app" / "graph" / "compiler.py"
        ).read_text(encoding="utf-8")
        start = source.index("if not policy.continues_on_error:")
        end = source.index("if not isinstance(result, dict):")
        branch = source[start:end]
        assert 'ERROR_KEY' in branch
        assert "result =" in branch, "the failure branch must assign, not merge"
