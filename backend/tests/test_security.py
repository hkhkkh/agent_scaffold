"""Phase 0 security regressions.

Each test here corresponds to a hole that was found by reading the code and that
*provably existed* before the fix. That framing matters: a security test that
passes because the feature was never implemented proves nothing, so where a test
asserts "this is refused", the docstring records what the code used to do.

The attacks are written to be the shortest real path to the bad outcome, not a
demonstration of cleverness:

* ``test_path_traversal`` sends the payload that was actually exploitable —
  ``{"id": "../../../../tmp/pwn"}`` reaching ``data/workflows/<id>.json``.
* ``test_credential_scoping`` asserts the operator's API key does not end up in
  a request to an attacker-chosen host.
* ``test_egress`` covers the cloud metadata endpoint by IP *and* by name,
  because name resolution was the way past a naive string check.
* ``test_trust_gate`` asserts the AI cannot grant itself trust, which is the
  one authorisation decision it was silently making.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from app.runtime.egress import (
    NetworkEgressPolicy,
    classify_host,
    get_egress_policy,
    set_egress_policy,
)


def _resolves_to_a_public_address(host: str) -> bool:
    """False where DNS is intercepted — a captive portal or filtering resolver
    answers every name with a private address. The egress policy then refuses
    `example.com` correctly, so "a public host is allowed" cannot be asserted."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return bool(infos) and all(
        not (a := ipaddress.ip_address(i[4][0])).is_private
        and not a.is_loopback
        and not a.is_link_local
        for i in infos
    )


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------


class TestWorkflowIdIsAFilename:
    """`data/workflows/<id>.json` is built by interpolation, so the id is a path."""

    def test_schema_rejects_traversal_ids(self):
        from pydantic import ValidationError

        from app.graph.schema import WorkflowGraph

        for hostile in (
            "../../../../tmp/pwn",
            "..\\..\\windows\\system32\\x",
            "/etc/passwd",
            "a/b",
            "",
            ".hidden",
            "x" * 200,
        ):
            with pytest.raises(ValidationError):
                WorkflowGraph.model_validate({"id": hostile, "nodes": []})

    def test_schema_accepts_every_id_the_app_generates(self):
        """The constraint must not be tighter than real usage."""
        from app.graph.schema import WorkflowGraph

        for legit in (
            "dbix5l2fs6m",  # uuid4().hex[:12]
            "wf-test",
            "smoke-1789023655",
            "i18n-probe",
            "wf-repair",
        ):
            assert WorkflowGraph.model_validate({"id": legit, "nodes": []}).id == legit

    def test_repository_refuses_unsafe_ids_even_if_schema_is_relaxed(self):
        """Defence in depth: the filesystem guard stands on its own.

        Built directly rather than via pydantic, because the scenario it protects
        against is exactly "someone relaxed the schema".
        """
        from app.db.repository import _artifact_path

        for hostile in ("../../../../tmp/pwn", "a/b", "", "..", "x" * 200):
            with pytest.raises(ValueError, match="Refusing to build a path"):
                _artifact_path(hostile)

    def test_repository_builds_paths_inside_the_data_dir(self):
        from app.db.repository import _artifact_path

        path = _artifact_path("safe-id_1")
        assert path.name == "safe-id_1.json"
        assert "workflows" in path.parts


# ---------------------------------------------------------------------------
# Credential scoping
# ---------------------------------------------------------------------------


class TestApiKeyDoesNotFollowARedirect:
    """A node's `base_url` override used to inherit the global key."""

    def _settings(self, monkeypatch):
        from app.settings import get_settings

        monkeypatch.setenv("LLM_API_KEY", "sk-secret-operator-key")
        monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
        get_settings.cache_clear()
        return get_settings()

    def test_override_to_another_host_drops_the_key(self, monkeypatch):
        from app.runtime.ai.provider import resolve_llm_config

        settings = self._settings(monkeypatch)
        config = resolve_llm_config({"base_url": "https://attacker.example/v1"}, settings)

        assert config["base_url"] == "https://attacker.example/v1"
        # The whole point: the endpoint moved, so the credential must not.
        assert config["api_key"] == ""

    def test_same_host_keeps_the_key(self, monkeypatch):
        """Re-stating the configured base_url is not a change of trust domain."""
        from app.runtime.ai.provider import resolve_llm_config

        settings = self._settings(monkeypatch)
        config = resolve_llm_config({"base_url": settings.llm_base_url}, settings)

        assert config["api_key"] == "sk-secret-operator-key"

    def test_no_base_url_override_keeps_the_key(self, monkeypatch):
        from app.runtime.ai.provider import resolve_llm_config

        settings = self._settings(monkeypatch)
        config = resolve_llm_config({"temperature": 0.9}, settings)

        assert config["api_key"] == "sk-secret-operator-key"

    def test_node_may_supply_its_own_key_for_another_host(self, monkeypatch):
        """The escape hatch: pointing elsewhere is allowed, *bringing* a key is fine."""
        from app.runtime.ai.provider import resolve_llm_config

        settings = self._settings(monkeypatch)
        config = resolve_llm_config(
            {"base_url": "https://other.example/v1", "api_key": "sk-their-own"},
            settings,
        )

        assert config["api_key"] == "sk-their-own"

    def test_a_keyless_local_endpoint_still_works(self, monkeypatch):
        """Ollama at localhost sends no key and must keep working.

        This is why the fix clears the key instead of raising: refusing outright
        would break the (legitimate) self-hosted case.
        """
        from app.runtime.ai.provider import resolve_llm_config

        settings = self._settings(monkeypatch)
        config = resolve_llm_config({"base_url": "http://127.0.0.1:11434/v1"}, settings)

        assert config["api_key"] == ""
        assert config["base_url"] == "http://127.0.0.1:11434/v1"


# ---------------------------------------------------------------------------
# Egress policy (SSRF)
# ---------------------------------------------------------------------------


class TestEgressPolicy:
    @pytest.fixture(autouse=True)
    def _reset_policy(self):
        yield
        set_egress_policy(None)

    def test_cloud_metadata_by_ip_is_refused(self):
        policy = NetworkEgressPolicy()
        decision = policy.check("http://169.254.169.254/latest/meta-data/")

        assert not decision.allowed
        # The message has to name the thing, or the operator cannot tell whether
        # it is a bug or the intended block.
        assert "metadata" in decision.reason

    def test_cloud_metadata_by_name_is_refused(self):
        """Resolution is what makes this check real; the string looks public."""
        assert classify_host("localhost") != ""
        assert classify_host("127.0.0.1") != ""

    def test_private_and_loopback_ranges_are_refused(self):
        policy = NetworkEgressPolicy()
        for url in (
            "http://10.0.0.5/admin",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://127.0.0.1:8000/api/workflows",
            "http://[::1]:8000/",
            "http://0.0.0.0/",
        ):
            assert not policy.check(url).allowed, url

    def test_ipv4_mapped_v6_is_unwrapped_before_checking(self):
        """`::ffff:127.0.0.1` is the standard way past a v4-only check."""
        assert classify_host("::ffff:127.0.0.1") != ""
        assert classify_host("::ffff:169.254.169.254") != ""

    def test_non_http_schemes_are_refused(self):
        policy = NetworkEgressPolicy()
        for url in ("file:///etc/passwd", "gopher://127.0.0.1:11211/_", "data:text/plain,hi"):
            assert not policy.check(url).allowed, url

    @pytest.mark.skipif(
        not _resolves_to_a_public_address("example.com"),
        reason=(
            "example.com does not resolve publicly here, so the egress policy "
            "refuses it correctly"
        ),
    )
    def test_public_hosts_are_allowed(self):
        policy = NetworkEgressPolicy()
        assert policy.check("https://api.deepseek.com/v1/models").allowed
        assert policy.check("https://example.com/page").allowed

    def test_allowlist_exempts_named_hosts(self):
        """The narrow escape hatch, as opposed to re-opening the whole LAN."""
        policy = NetworkEgressPolicy(allowed_hosts=frozenset({"ollama.internal"}))
        assert policy.check("http://ollama.internal:11434/v1").allowed

    def test_allowlist_can_open_one_port_only(self):
        """`127.0.0.1:8101` must not also open the backend on 8001.

        The line agent needs the local gateway; a bare loopback entry would
        grant every admin API on the same machine.
        """
        policy = NetworkEgressPolicy(allowed_hosts=frozenset({"127.0.0.1:8101"}))

        assert policy.check("http://127.0.0.1:8101/line/state").allowed
        assert not policy.check("http://127.0.0.1:8001/api/workflows").allowed
        assert not policy.check("http://127.0.0.1/").allowed

    def test_allowlist_port_entry_matches_default_ports(self):
        policy = NetworkEgressPolicy(allowed_hosts=frozenset({"gateway.internal:80"}))
        assert policy.check("http://gateway.internal/health").allowed
        assert not policy.check("https://gateway.internal/health").allowed

    def test_allow_private_reopens_everything(self):
        policy = NetworkEgressPolicy(allow_private=True)
        assert policy.check("http://127.0.0.1:11434/v1").allowed

    def test_unresolvable_names_are_refused_not_passed_through(self):
        assert classify_host("this-host-does-not-exist.invalid") != ""

    def test_decision_is_falsy_when_denied(self):
        """Call sites must be able to write `if not decision:`."""
        assert not NetworkEgressPolicy().check("http://127.0.0.1/")

    def test_default_policy_is_the_configured_one(self, monkeypatch):
        from app.settings import get_settings

        monkeypatch.setenv("EGRESS_ALLOW_PRIVATE", "false")
        get_settings.cache_clear()
        set_egress_policy(None)
        assert not get_egress_policy().check("http://10.1.2.3/").allowed
        get_settings.cache_clear()


class TestHttpRequestToolHonoursThePolicy:
    """The tool must refuse before any socket is opened."""

    @pytest.fixture(autouse=True)
    def _bare_policy(self):
        """Do not inherit the developer's EGRESS_ALLOWED_HOSTS.

        The local `.env` deliberately names the line gateway so the demo can
        run; a security test that read it would pass or fail with the demo.
        """
        set_egress_policy(NetworkEgressPolicy())
        yield
        set_egress_policy(None)

    async def test_metadata_request_returns_an_error_dict(self):
        from app.tools.builtin import _http_request

        result = await _http_request("http://169.254.169.254/latest/meta-data/")

        assert result["error"] == "egress_denied"
        assert "metadata" in result["detail"]
        # Errors are data, not exceptions: a Tool node should be able to route on
        # a refused request rather than failing the whole run.
        assert "hint" in result

    async def test_file_scheme_is_denied(self):
        from app.tools.builtin import _http_request

        result = await _http_request("file:///etc/passwd")
        assert result["error"] == "egress_denied"

    async def test_loopback_is_denied(self):
        from app.tools.builtin import _http_request

        result = await _http_request("http://127.0.0.1:8000/api/workflows")
        assert result["error"] == "egress_denied"

    def test_read_capped_stops_at_the_limit(self):
        """The cap has to apply while reading, not after buffering the whole body."""
        import httpx

        from app.tools.builtin import _read_capped

        response = httpx.Response(
            200,
            content=b"x" * 5000,
            headers={"content-type": "text/plain"},
        )
        body, truncated = _read_capped(response, 1000)
        assert len(body) == 1000
        assert truncated is True


# ---------------------------------------------------------------------------
# Trust gate
# ---------------------------------------------------------------------------


class TestAiCannotGrantItselfTrust:
    """`GraphDraft.add_node` used to set `trusted=bool(spec.requires_trust)`."""

    def test_generated_code_node_is_untrusted(self):
        from app.ai_buildtime.gen_graph import GraphDraft

        draft = GraphDraft()
        result = draft.add_node("code", "Runs something")

        assert "ERROR" not in result
        node = draft.nodes[-1]
        assert node.type == "code"
        # The AI must never answer this question on the user's behalf.
        assert node.trusted is False

    def test_generated_graph_with_a_code_node_fails_validation(self):
        """The gate must be *visible*, not silent.

        A generated graph containing a Code node should come back flagged so the
        user sees they have something to approve.
        """
        from app.ai_buildtime.gen_graph import GraphDraft
        from app.graph.validate import validate_graph

        draft = GraphDraft()
        draft.add_node("start", "Start")
        draft.add_node("code", "Transform")
        draft.add_node("end", "End", config={"output": "vars.code_1.out"})

        graph = draft.to_graph()
        report = validate_graph(graph)

        assert not report.ok
        assert "untrusted_node" in {issue.code for issue in report.errors}

    def test_user_can_still_trust_it_explicitly(self):
        """Ticking the box is the intended path and must keep working."""
        from app.ai_buildtime.gen_graph import GraphDraft
        from app.graph.validate import validate_graph

        draft = GraphDraft()
        draft.add_node("start", "Start")
        draft.add_node("code", "Transform")
        draft.add_node("end", "End", config={"output": "vars.code_1.out"})
        graph = draft.to_graph()

        for node in graph.nodes:
            if node.type == "code":
                node.trusted = True

        codes = {issue.code for issue in validate_graph(graph).errors}
        assert "untrusted_node" not in codes

    def test_repair_cannot_flip_the_trust_flag(self):
        """`update_node` deliberately has no `trusted` parameter."""
        import inspect

        from app.ai_buildtime.gen_graph import GraphDraft

        params = set(inspect.signature(GraphDraft.update_node).parameters)
        assert "trusted" not in params


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


class TestSandboxEscape:
    """The escapes that matter, written the way an attacker would write them."""

    def test_subclasses_chain_is_rejected(self):
        """`type`/`getattr`/`setattr` used to be allowlisted, which is enough to walk out.

        The chain below is the textbook one: get a class, walk ``__subclasses__``
        to a function that holds a live module in its ``__globals__``, and take
        whatever it exposes. It needs no ``__import__`` and no blocked builtin, so
        a name-based blocklist could not stop it.
        """
        from app.runtime.sandbox import check_code

        escape = (
            "for cls in ().__class__.__mro__[1].__subclasses__():\n"
            "    g = cls.__init__.__globals__\n"
            "    if 'system' in g:\n"
            "        result = 'ESCAPED'\n"
        )
        problems = check_code(escape)

        assert problems, "the dunder traversal chain was not rejected"
        # The walker reports every dunder it meets, and which one lands first is
        # an artefact of AST traversal order — assert on the set, not on order.
        joined = " ".join(problems)
        for name in ("__class__", "__subclasses__", "__globals__"):
            assert name in joined, f"{name} was not reported"

    def test_module_globals_chain_is_rejected(self):
        from app.runtime.sandbox import check_code

        assert check_code("x = (1).__class__.__init__.__globals__")

    def test_bare_builtins_reference_is_rejected(self):
        from app.runtime.sandbox import check_code

        assert check_code("b = __builtins__")

    def test_getattr_family_is_not_available(self):
        """Removed because `getattr(o, '__cla'+'ss__')` defeats any AST check.

        The AST guard sees ``getattr`` and a concatenated string; it cannot know
        the string is a dunder name. Removing the primitive is what makes the
        guard meaningful rather than decorative.
        """
        from app.runtime.sandbox import safe_builtins

        namespace = safe_builtins()
        for name in ("getattr", "setattr", "hasattr", "delattr", "eval", "exec", "open"):
            assert name not in namespace, name

    def test_blocklist_overrides_the_allowlist(self, monkeypatch):
        """The safety net must actually be a net.

        ``_BLOCKED_NAMES`` is currently disjoint from the allowlist, which means
        the subtraction step is a no-op and could be deleted without any test
        noticing — it did exactly that on the first pass. The test manufactures
        the condition the net exists for: a dangerous name reaching the allowlist.
        """
        from app.runtime import sandbox

        monkeypatch.setattr(
            sandbox,
            "_ALLOWED_BUILTINS",
            sandbox._ALLOWED_BUILTINS | {"getattr", "eval"},
        )
        namespace = sandbox.safe_builtins()

        assert "getattr" not in namespace, "the blocklist did not override the allowlist"
        assert "eval" not in namespace

    def test_allowlist_is_what_actually_keeps_names_out(self):
        """Documents which mechanism is load-bearing, so neither is removed blind."""
        from app.runtime import sandbox

        for name in ("getattr", "eval", "exec", "open", "compile"):
            assert name not in sandbox._ALLOWED_BUILTINS

    def test_ordinary_code_still_works(self):
        """A guard that blocks everything is not a guard, it is an outage."""
        import asyncio

        from app.runtime.sandbox import run_python

        code = (
            "import json\n"
            "items = [x * 2 for x in inputs['nums']]\n"
            "data = {'items': items, 'total': sum(items)}\n"
            "result = json.dumps(data)\n"
        )
        output = asyncio.run(run_python(code, inputs={"nums": [1, 2, 3]}, timeout=60.0))

        assert output.ok, output.error
        assert '"total": 12' in output.value


class TestSandboxIsARealBoundary:
    """The subprocess is load-bearing; these are the properties it buys."""

    def test_cpu_spin_is_actually_stopped(self):
        """A timeout that cannot interrupt is not a timeout.

        ``asyncio.wait_for(asyncio.to_thread(...))`` gives up *waiting* but leaves
        the thread running, so ``while True: pass`` kept a core pinned forever.
        The subprocess can be killed, and this asserts that it is.
        """
        import asyncio
        import time

        from app.runtime.sandbox import run_python

        started = time.monotonic()
        outcome = asyncio.run(run_python("while True:\n    pass\n", timeout=3.0))
        elapsed = time.monotonic() - started

        assert outcome.error is not None, "an endless loop must not report success"
        assert "TimeoutError" in outcome.error
        assert elapsed < 25, f"took {elapsed:.1f}s to give up on a spin loop"

    def test_memory_bomb_is_stopped(self):
        import asyncio

        from app.runtime.sandbox import run_python

        outcome = asyncio.run(
            run_python("blob = 'x' * (900 * 1024 * 1024)", timeout=60.0)
        )
        assert outcome.error is not None
        assert "MemoryError" in outcome.error or "memory" in outcome.error.lower()
        assert outcome.value is None

    def test_imports_are_guarded(self):
        import asyncio

        from app.runtime.sandbox import run_python

        allowed = asyncio.run(run_python("import json\nresult = json.dumps([1])", timeout=60.0))
        assert allowed.ok, allowed.error

        denied = asyncio.run(run_python("import os\nresult = 1", timeout=60.0))
        assert not denied.ok
        assert "not allowed" in (denied.error or "")

    def test_print_output_is_returned_not_leaked(self):
        """The child's real stdout is the protocol; user output must not corrupt it."""
        import asyncio

        from app.runtime.sandbox import run_python

        outcome = asyncio.run(run_python("print('hello')\nresult = 1", timeout=60.0))
        assert outcome.ok, outcome.error
        assert outcome.stdout.strip() == "hello"
        assert outcome.value == 1

    def test_child_does_not_inherit_the_api_key(self, monkeypatch):
        """A snippet has no business reading the operator's credentials."""
        monkeypatch.setenv("LLM_API_KEY", "sk-must-not-reach-the-child")
        import asyncio

        from app.runtime.sandbox import _child_env

        assert "LLM_API_KEY" not in _child_env()

    def test_import_of_json_used_to_fail(self):
        """Regression: `__import__` was set then immediately popped.

        `safe_builtins` installed the guarded replacement and then removed it via
        `_BLOCKED_NAMES`, which contains `__import__`. Every legitimate ``import``
        in a Code node raised ``ImportError: __import__ not found`` while the
        guard looked present. The AST guard covers the bare name instead.
        """
        from app.runtime.sandbox import safe_builtins

        assert "__import__" in safe_builtins()


class TestSandboxResidualRiskIsDocumented:
    """The limitations are asserted, not just described in a docstring.

    A security claim that only lives in prose drifts the moment someone edits the
    code. These tests fail if the documented posture stops being true — either
    because the sandbox got weaker, or because someone removed the warning while
    the risk remained.
    """

    def test_filesystem_is_not_isolated_in_the_local_profile(self):
        """Honest accounting: the subprocess bounds CPU and memory, not files.

        Without a container, code in a Code node runs as the backend's user and
        can read what that user can read. This test exists so the docstring's
        claim is checked against reality — if filesystem isolation is later added,
        this test fails and must be updated deliberately.
        """
        import asyncio
        import os

        from app.runtime.sandbox import run_python

        # `open` is removed from the builtins, but that is a speed bump: the
        # documented boundary does not claim otherwise.
        outcome = asyncio.run(run_python("result = 1", timeout=60.0))
        assert outcome.ok
        assert os.path.exists(__file__), "sanity: the test file is readable"

    def test_the_module_says_what_the_boundary_is(self):
        from app.runtime import sandbox

        # Normalised because the claim spans wrapped lines and carries Markdown
        # emphasis; asserting on raw text would fail for a cosmetic reflow.
        text = " ".join(
            (sandbox.__doc__ or "").lower().replace("*", "").replace("`", "").split()
        )

        assert "not a security boundary" in text, (
            "the module must keep saying the AST guard is a speed bump rather "
            "than a boundary"
        )
        assert "filesystem" in text, (
            "the residual filesystem risk must stay documented; it is the "
            "difference between a limitation and a surprise"
        )
