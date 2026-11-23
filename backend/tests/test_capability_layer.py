"""Phase 1 capability layer — the seam between nodes and the platform.

What these tests are actually defending
---------------------------------------

Phase 0 established a security control (auth middleware) that was correct,
thoroughly tested, and **not installed in the product**. Every test passed
because each one built its own object and never looked at the real application.
The lesson was written down as "tests must assert on the real application
object for anything that only works when installed".

This file applies that lesson to the capability layer. The valuable assertion is
not "the runner works" — Phase 0 already tested the child-process machinery.
It is **"the node goes through the interface"**. A node that imports the
implementation directly would still pass every sandbox test while making the
whole abstraction decorative.

So the first class here installs a runner that records its calls, runs a real
`CodeNode`, and asserts the node used it. Swap `get_sandbox_runner().run(...)`
in `nodes/code.py` back to `run_python(...)` and that test fails — which is
exactly what ``_guardprobe_capability.py`` checks.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from app.graph.schema import NodeSpec
from app.nodes.base import NodeContext
from app.runtime.runner import (
    CONTAINER_BOOTSTRAP,
    ContainerSandboxRunner,
    LocalSandboxRunner,
    SandboxConfigError,
    SandboxRunner,
    build_sandbox_runner,
    get_sandbox_runner,
    set_sandbox_runner,
)
from app.runtime.sandbox import DEFAULT_MEMORY_LIMIT, CodeResult
from app.settings import Settings


@pytest.fixture(autouse=True)
def _restore_runner():
    """The runner is process-wide, so every test in this file must restore it."""
    yield
    set_sandbox_runner(None)


def _settings(**overrides: Any) -> Settings:
    """Deterministic settings: never read the developer's `.env`."""
    return Settings(_env_file=None, **overrides)


def _node_context(
    code: str,
    *,
    settings: Settings | None = None,
    trusted: bool = True,
    config: dict[str, Any] | None = None,
) -> tuple[NodeContext, list[tuple[Any, ...]]]:
    events: list[tuple[Any, ...]] = []
    merged = {"code": code, **(config or {})}

    async def emit(payload: dict[str, Any]) -> None:
        events.append(("emit", payload))

    async def log(text: str, level: str = "info") -> None:
        events.append(("log", text, level))

    context = NodeContext(
        node=NodeSpec(
            id="n1", type="code", label="Code", trusted=trusted, config=merged
        ),
        config=merged,
        inputs={"in": "hello"},
        state={"vars": {}},
        settings=settings or _settings(),
        emit=emit,
        log=log,
    )
    return context, events


class _RecordingRunner(SandboxRunner):
    """A second implementation, used to prove the first one is replaceable."""

    def __init__(self, result: CodeResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or CodeResult(value={"from": "recording runner"})

    async def run(
        self,
        code: str,
        *,
        inputs: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        vars_: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> CodeResult:
        self.calls.append(
            {
                "code": code,
                "inputs": inputs,
                "state": state,
                "vars": vars_,
                "params": params,
                "timeout": timeout,
            }
        )
        return self.result


# ---------------------------------------------------------------------------
# The seam is live
# ---------------------------------------------------------------------------


class TestCodeNodeGoesThroughTheCapabilityLayer:
    """Not "the sandbox works" — "the node asks the platform, not the module"."""

    def test_a_replaced_runner_is_the_one_that_executes(self):
        from app.nodes.code import CodeNode

        runner = _RecordingRunner()
        set_sandbox_runner(runner)
        context, _ = _node_context("result = 42")

        output = asyncio.run(CodeNode(context.node).run(context))

        assert len(runner.calls) == 1, "the node did not use the installed runner"
        assert runner.calls[0]["code"] == "result = 42"

    def test_the_runner_result_is_what_flows_downstream(self):
        """A runner that is called but ignored would be worse than no seam.

        The node must adopt both halves of the contract — the value and the
        captured stdout — or replacing the runner would change nothing visible.
        """
        from app.nodes.code import CodeNode

        runner = _RecordingRunner(
            CodeResult(value={"answer": 42}, stdout="printed by the runner")
        )
        set_sandbox_runner(runner)
        context, events = _node_context("result = 1")

        output = asyncio.run(CodeNode(context.node).run(context))

        assert output["out"] == {"answer": 42}
        assert output["stdout"] == "printed by the runner"
        assert any(
            event[:2] == ("log", "printed by the runner") for event in events
        ), "the runner's stdout never reached the run timeline"

    def test_the_node_passes_the_graph_data_it_promised(self):
        """The Code node's documented contract is inputs/state/vars/params."""
        from app.nodes.code import CodeNode

        runner = _RecordingRunner()
        set_sandbox_runner(runner)
        context, _ = _node_context("result = 1", config={"fail_on_error": False})

        asyncio.run(CodeNode(context.node).run(context))
        call = runner.calls[0]

        assert call["inputs"] == {"in": "hello"}
        assert call["state"] == {"vars": {}}
        assert call["params"]["fail_on_error"] is False

    def test_the_node_timeout_setting_is_the_budget_handed_to_the_runner(self):
        from app.nodes.code import CodeNode

        runner = _RecordingRunner()
        set_sandbox_runner(runner)
        context, _ = _node_context("result = 1", settings=_settings(node_timeout_seconds=7.5))

        asyncio.run(CodeNode(context.node).run(context))

        assert runner.calls[0]["timeout"] == 7.5

    def test_switching_the_runner_changes_what_a_run_fails_with(self):
        """End-to-end proof that the swap is behavioural, not cosmetic."""
        from app.nodes.code import CodeNode

        class _Refusing(SandboxRunner):
            async def run(self, code, **kwargs):  # type: ignore[override]
                return CodeResult(error="refused by the second implementation")

        set_sandbox_runner(_Refusing())
        context, _ = _node_context("result = 1")

        with pytest.raises(ValueError, match="refused by the second implementation"):
            asyncio.run(CodeNode(context.node).run(context))


# ---------------------------------------------------------------------------
# Choosing a runner
# ---------------------------------------------------------------------------


class TestRunnerSelection:
    def test_local_is_the_default(self):
        assert isinstance(build_sandbox_runner(_settings()), LocalSandboxRunner)

    def test_container_is_selected_by_configuration(self):
        runner = build_sandbox_runner(
            _settings(sandbox_runner="container", sandbox_image="example/sandbox:1")
        )
        assert isinstance(runner, ContainerSandboxRunner)
        assert runner.image == "example/sandbox:1"

    def test_an_unknown_runner_is_an_error_not_a_silent_downgrade(self):
        """The failure mode that matters: a typo must not remove the isolation.

        ``SANDBOX_RUNNER=containers`` resolving to "run it locally" would leave an
        operator believing snippets are containerised when they are not. Refusing
        to start is the only honest answer.
        """
        with pytest.raises(SandboxConfigError, match="not a known runner"):
            build_sandbox_runner(_settings(sandbox_runner="containers"))

    def test_the_memory_setting_becomes_the_runner_limit(self):
        runner = build_sandbox_runner(_settings(sandbox_memory_bytes=64 * 1024 * 1024))
        assert runner.memory_limit == 64 * 1024 * 1024

    def test_a_zero_memory_setting_falls_back_to_the_default(self):
        """0 is the "unset" value, not a request for a zero-byte sandbox."""
        runner = build_sandbox_runner(_settings(sandbox_memory_bytes=0))
        assert runner.memory_limit == DEFAULT_MEMORY_LIMIT

    def test_the_getter_is_lazy_and_the_setter_restores(self):
        assert isinstance(get_sandbox_runner(), SandboxRunner)

        replacement = _RecordingRunner()
        set_sandbox_runner(replacement)
        assert get_sandbox_runner() is replacement

        set_sandbox_runner(None)
        assert get_sandbox_runner() is not replacement

    def test_both_implementations_satisfy_the_interface(self):
        """Cheap, but it catches a subclass that drifted off the ABC."""
        for runner in (LocalSandboxRunner(), ContainerSandboxRunner()):
            assert isinstance(runner, SandboxRunner)
            assert callable(runner.run)

    def test_the_default_runner_still_runs_ordinary_code(self):
        """The abstraction must not have broken the feature it abstracts.

        Every other test in this file could pass while the default runner was
        wired to nothing. This is the one that says a Code node still works.
        """
        code = "import json\nresult = json.dumps({'len': len(inputs['in']), 'ok': True})"

        outcome = asyncio.run(
            get_sandbox_runner().run(code, inputs={"in": "hello"}, timeout=60.0)
        )

        assert outcome.ok, outcome.error
        assert '"len": 5' in outcome.value


# ---------------------------------------------------------------------------
# The memory limit is enforced, not merely stored
# ---------------------------------------------------------------------------


class TestMemoryLimitIsNotDecorative:
    """A parameter that is accepted and ignored is the classic dead guard.

    `run_python` used to read `SANDBOX_MEMORY_BYTES` from the environment, so a
    runner holding a limit could do nothing with it. These tests would pass
    before *and* after that bug, unless they compare two limits against the same
    snippet — which is why they do.
    """

    _SIZEABLE = "blob = 'x' * (300 * 1024 * 1024)\nresult = len(blob)"

    def test_the_same_snippet_survives_a_loose_limit_and_dies_at_a_tight_one(self):
        tight = asyncio.run(
            LocalSandboxRunner(memory_limit=128 * 1024 * 1024).run(
                self._SIZEABLE, timeout=60.0
            )
        )
        loose = asyncio.run(
            LocalSandboxRunner(memory_limit=1024 * 1024 * 1024).run(
                self._SIZEABLE, timeout=60.0
            )
        )

        assert not tight.ok, "128 MB admitted a 300 MB allocation"
        assert "MemoryError" in (tight.error or "")
        assert loose.ok, loose.error
        assert loose.value == 300 * 1024 * 1024

    def test_run_python_honours_an_explicit_limit(self):
        from app.runtime.sandbox import run_python

        outcome = asyncio.run(
            run_python(
                "blob = 'x' * (300 * 1024 * 1024)\nresult = 1",
                timeout=60.0,
                memory_limit=128 * 1024 * 1024,
            )
        )
        assert not outcome.ok
        assert outcome.value is None
        assert "MemoryError" in (outcome.error or "")

    def test_a_memory_failure_never_reports_an_empty_explanation(self):
        """Regression: the message used to be exactly ``"MemoryError: "``.

        ``str(MemoryError())`` is the empty string, so the child's generic
        ``f"{type(exc).__name__}: {exc}"`` handler produced an error with no
        information in it — the author saw that the snippet failed and nothing
        about why. Found while parameterising the memory limit in Phase 1, and
        only because the test compared two limits rather than asserting success.

        Asserting on message *content* rather than presence is the point: the
        broken version did have a message, it just said nothing.
        """
        from app.runtime.sandbox import run_python

        outcome = asyncio.run(
            run_python(
                "blob = 'x' * (300 * 1024 * 1024)\nresult = 1",
                timeout=60.0,
                memory_limit=128 * 1024 * 1024,
            )
        )

        error = outcome.error or ""
        assert error.startswith("MemoryError")
        assert "ran out of memory" in error, f"uninformative memory error: {error!r}"


# ---------------------------------------------------------------------------
# Container profile
# ---------------------------------------------------------------------------


class TestContainerCommandShape:
    """The flags *are* the isolation, so they are asserted without a daemon.

    A test that needs Docker to observe these would never run in CI, and the
    options are the part most likely to be lost in a refactor.
    """

    def _argv(self, **kwargs: Any) -> list[str]:
        return ContainerSandboxRunner(**kwargs).command("probe-container")

    @staticmethod
    def _value(argv: list[str], flag: str) -> str:
        assert flag in argv, f"{flag} missing from {argv}"
        return argv[argv.index(flag) + 1]

    def test_container_gets_no_network(self):
        """The local profile cannot offer this at all."""
        assert self._value(self._argv(), "--network") == "none"

    def test_filesystem_is_read_only_with_a_bounded_tmp(self):
        argv = self._argv()
        assert "--read-only" in argv

        tmpfs = self._value(argv, "--tmpfs")
        assert tmpfs.startswith("/tmp:")
        assert "size=16m" in tmpfs
        # noexec matters: the writable path must not become an execution path.
        assert "noexec" in tmpfs

    def test_swap_is_capped_equal_to_memory(self):
        """Otherwise the ceiling is advisory — the kernel pages the rest to swap."""
        argv = self._argv(memory_limit=256 * 1024 * 1024)
        assert self._value(argv, "--memory") == "256m"
        assert self._value(argv, "--memory-swap") == "256m"

    def test_capabilities_are_dropped_entirely(self):
        argv = self._argv()
        assert self._value(argv, "--cap-drop") == "ALL"
        assert self._value(argv, "--security-opt") == "no-new-privileges"

    def test_snippets_run_as_an_unprivileged_user(self):
        argv = self._argv()
        assert self._value(argv, "--user").startswith("65534")
        assert self._value(argv, "--pids-limit") == "64"

    def test_the_container_is_named_so_it_can_be_killed(self):
        argv = self._argv()
        assert self._value(argv, "--name") == "probe-container"
        assert "--rm" in argv, "a leaked container would accumulate per run"

    def test_the_payload_travels_over_stdin_not_argv(self):
        """The snippet must not appear in the host process list."""
        argv = self._argv()
        assert "--interactive" in argv
        assert tuple(argv[-4:-1]) == ("python3", "-I", "-c")
        assert not any("inputs" in part for part in argv)

    def test_the_container_is_told_where_the_package_lives(self):
        """Regression-in-waiting: ``-I`` implies ``-P``, so ``/workspace`` is
        not on ``sys.path`` by default.

        Without the explicit insert the container starts fine and every snippet
        dies with ``ModuleNotFoundError: No module named 'app'`` — which reads
        like a broken image rather than a missing one-line bootstrap. The local
        child process needed the identical fix, so this is not a hypothetical.
        """
        argv = self._argv()
        bootstrap = argv[-1]

        assert "-I" in argv
        assert "sys.path.insert" in bootstrap
        assert "'/workspace'" in bootstrap
        assert "sandbox_child" in bootstrap

    def test_the_bootstrap_follows_the_working_directory(self):
        """An operator relocating the package must not silently break imports."""
        runner = ContainerSandboxRunner(workdir="/opt/agent")
        argv = runner.command("probe-container")

        assert self._value(argv, "--workdir") == "/opt/agent"
        assert "'/opt/agent'" in argv[-1]

    def test_the_local_and_container_profiles_inject_the_path_the_same_way(self):
        """Both must carry the bootstrap; only one of them is exercised by default."""
        from app.runtime.sandbox import _CHILD_BOOTSTRAP as local_bootstrap

        assert "sys.path.insert" in local_bootstrap
        assert "sys.path.insert" in ContainerSandboxRunner().entrypoint()[3]


class TestContainerFailsClosed:
    """The one behaviour that must never be "helpful"."""

    def test_a_missing_cli_refuses_rather_than_running_locally(self):
        runner = ContainerSandboxRunner(docker="agent-scaffold-no-such-cli-xyz")

        ok, reason = asyncio.run(runner.available())
        assert not ok
        assert "not found" in reason

        outcome = asyncio.run(runner.run("result = 1", timeout=5.0))
        assert not outcome.ok
        assert outcome.value is None
        assert "NOT run" in (outcome.error or "")
        assert "SANDBOX_RUNNER=local" in (outcome.error or "")

    def test_an_unreachable_daemon_is_reported_as_such(self):
        """Uses the interpreter as a stand-in for a Docker CLI with no daemon.

        Deterministic and portable: ``python version --format ...`` exits
        non-zero on every platform, which is the shape of "the CLI is there but
        the daemon is not".
        """
        runner = ContainerSandboxRunner(docker=sys.executable)

        ok, reason = asyncio.run(runner.available())
        assert not ok
        assert "unreachable" in reason

    def test_the_guard_runs_before_the_daemon_is_consulted(self):
        """A snippet the AST guard rejects must not cost a container start."""
        runner = ContainerSandboxRunner(docker="agent-scaffold-no-such-cli-xyz")

        outcome = asyncio.run(runner.run("result = ().__class__", timeout=5.0))

        assert not outcome.ok
        assert "SandboxViolation" in (outcome.error or "")
        assert "not found" not in (outcome.error or ""), (
            "the daemon check ran first, so the guard was bypassed by ordering"
        )
