"""Security and authentication messages in both languages (task 12).

The point of these is not that Chinese exists -- it is that English still does,
in exactly the place where it matters. A denied tool call travels back to a
**model**, and the contract is that text shown to a model is always English, no
matter what language the person operating the canvas picked. A test that only
checked "the Chinese is there" would pass while that contract was broken.
"""

from __future__ import annotations

import json
import ipaddress
import re
import socket

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.runtime.ai.messages import missing_translations
from app.runtime.egress import _DENY_MESSAGES, _HOST_REASONS, NetworkEgressPolicy
from app.security import deps
from app.security.auth import Principal, PUBLIC_PATHS
from app.security.deps import AuthMiddleware, configure


def _resolves_to_a_public_address(host: str) -> bool:
    """False where DNS is intercepted, so `example.com` answers with a private
    address and the egress policy refuses it correctly. Same guard as
    `tests/test_security.py`."""
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

LOOPBACK_PEER = ("127.0.0.1", 54321)


# ---------------------------------------------------------------------------
# The tables themselves
# ---------------------------------------------------------------------------


class TestTablesAreComplete:
    @pytest.mark.parametrize(
        "table",
        [_DENY_MESSAGES, deps._MESSAGES],
        ids=["egress", "auth"],
    )
    def test_every_message_has_both_languages(self, table):
        """The mechanism, not the habit: a message added in English alone fails
        here rather than in front of a user who reads Chinese."""
        assert missing_translations(table) == []

    def test_every_host_fragment_has_a_translation(self):
        """Keyed by the English fragment, so the values are what carry Chinese."""
        assert all(text for text in _HOST_REASONS.values())

    def test_the_placeholder_sets_match_between_languages(self):
        """A translation that drops `{scope}` would render literally and look
        finished."""
        for table_name, table in (("egress", _DENY_MESSAGES), ("auth", deps._MESSAGES)):
            for key, entry in table.items():
                en = set(re.findall(r"\{(\w+)\}", entry["en"]))
                zh = set(re.findall(r"\{(\w+)\}", entry["zh"]))
                assert en == zh, f"{table_name}.{key}: {en} vs {zh}"

    def test_python_has_a_renderable_table_entry_per_denial_key(self):
        """`_deny` is called with a literal key; a typo would raise KeyError at
        request time, so the keys used in the module are enumerated here."""
        import pathlib

        source = pathlib.Path(deps.__file__).parents[1] / "runtime" / "egress.py"
        used = set(re.findall(r'_deny\(\s*"(\w+)"', source.read_text(encoding="utf-8")))
        assert used, "no _deny calls found -- did the call sites change?"
        assert used <= set(_DENY_MESSAGES), f"unknown keys: {used - set(_DENY_MESSAGES)}"


# ---------------------------------------------------------------------------
# Egress: the two consumers
# ---------------------------------------------------------------------------


class TestEgressSpeaksBoth:
    def test_a_loopback_target_is_denied_in_english_and_chinese(self):
        decision = NetworkEgressPolicy().check("http://127.0.0.1/x")
        assert decision.allowed is False
        assert "loopback" in decision.reason
        assert "回环地址" in decision.reason_localized

    def test_the_chinese_sentence_has_no_english_left_in_it(self):
        """The failure this catches is the one that looks finished: a Chinese
        sentence with an English clause in the middle."""
        decision = NetworkEgressPolicy().check("http://10.0.0.5/x")
        assert not re.search(r"[a-z]{4,}", decision.reason_localized.split("：")[1]), (
            f"English left in the Chinese sentence: {decision.reason_localized!r}"
        )

    def test_the_longest_host_fragment_wins(self):
        """`the cloud instance-metadata endpoint (...)` contains no shorter
        fragment, but the table is scanned longest-first so a future overlapping
        pair cannot half-replace."""
        decision = NetworkEgressPolicy().check("http://169.254.169.254/latest")
        assert "云实例元数据端点" in decision.reason_localized
        assert "169.254.169.254" in decision.reason_localized

    @pytest.mark.skipif(
        not _resolves_to_a_public_address("example.com"),
        reason=(
            "example.com does not resolve publicly here, so the egress policy "
            "refuses it correctly"
        ),
    )
    def test_an_allow_decision_has_no_reason_in_either_language(self):
        decision = NetworkEgressPolicy().check("http://example.com")
        assert decision.allowed is True
        assert decision.reason == ""
        assert decision.reason_localized == ""

    def test_the_english_reason_is_exactly_what_it_was(self):
        """Pinned because this string reaches a model: changing it is a behaviour
        change, not a translation change."""
        decision = NetworkEgressPolicy().check("http://127.0.0.1/x")
        assert decision.reason == (
            "refusing to request 127.0.0.1: it is a loopback address. "
            "Add it to EGRESS_ALLOWED_HOSTS if this is intentional."
        )


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


def _app(**kwargs) -> FastAPI:
    """A minimal app carrying the real middleware and a protected route."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/workflows")
    def list_workflows():
        return {"workflows": []}

    @app.get("/api/admin")
    def admin_only():
        return {"ok": True}

    return app


class TestAuthSpeaksBoth:
    def test_a_missing_token_is_refused_in_english_by_default(self):
        configure(mode="token", host="0.0.0.0", token_specs=["alice:secret"])
        client = TestClient(_app(), client=LOOPBACK_PEER)
        response = client.get("/api/workflows")
        assert response.status_code == 401
        assert response.json()["detail"] == "A valid bearer token is required."

    def test_a_missing_token_is_refused_in_chinese_when_asked(self):
        configure(mode="token", host="0.0.0.0", token_specs=["alice:secret"])
        client = TestClient(_app(), client=LOOPBACK_PEER)
        response = client.get("/api/workflows?lang=zh")
        assert response.status_code == 401
        assert response.json()["detail"] == "需要有效的 bearer token。"
        assert "Bearer" in response.json()["hint"]

    def test_an_insufficient_scope_is_refused_in_chinese_when_asked(self):
        configure(mode="token", host="0.0.0.0", token_specs=["alice:secret:read"])
        client = TestClient(_app(), client=LOOPBACK_PEER)
        response = client.post(
            "/api/workflows?lang=zh", headers={"Authorization": "Bearer secret"}
        )
        assert response.status_code == 403
        assert "权限" in response.json()["detail"]
        assert "write" in response.json()["detail"]

    def test_the_language_header_is_honoured_too(self):
        """`Accept-Language` is what a browser sends without being asked; the
        canvas uses `?lang=`."""
        configure(mode="token", host="0.0.0.0", token_specs=["alice:secret"])
        client = TestClient(_app(), client=LOOPBACK_PEER)
        response = client.get("/api/workflows", headers={"Accept-Language": "zh-CN,zh;q=0.9"})
        assert response.status_code == 401
        assert response.json()["detail"] == "需要有效的 bearer token。"

    def test_local_mode_refusal_is_translated(self):
        """The message that tells an operator their bind is wrong."""
        configure(mode="local", host="127.0.0.1")
        client = TestClient(_app(), client=("192.168.1.50", 54321))
        response = client.get("/api/workflows?lang=zh")
        assert response.status_code == 401
        assert "本地模式" in response.json()["detail"]

    def test_the_query_parameter_beats_the_header(self):
        """Explicit beats ambient: the canvas sets `?lang=` for the language the
        user chose, which is more specific than what the browser prefers."""
        configure(mode="token", host="0.0.0.0", token_specs=["alice:secret"])
        client = TestClient(_app(), client=LOOPBACK_PEER)
        response = client.get(
            "/api/workflows?lang=en", headers={"Accept-Language": "zh-CN"}
        )
        assert response.json()["detail"] == "A valid bearer token is required."

    def test_an_unknown_language_falls_back_to_english(self):
        configure(mode="token", host="0.0.0.0", token_specs=["alice:secret"])
        client = TestClient(_app(), client=LOOPBACK_PEER)
        response = client.get("/api/workflows?lang=klingon")
        assert response.json()["detail"] == "A valid bearer token is required."

    @pytest.mark.parametrize("path", sorted(PUBLIC_PATHS))
    def test_a_public_path_is_never_refused(self, path):
        """The rule from the task: the public paths are not part of the
        authenticated surface, so their responses carry no auth message at all.

        Asserted by status rather than by wording, because the failure it guards
        against is the middleware starting to gate something it should not --
        which would look like a 401 in *some* language, not like a wrong string.
        """
        configure(mode="token", host="0.0.0.0", token_specs=["alice:secret"])
        app = _app()

        @app.get(path)
        def public():
            return {"ok": True}

        client = TestClient(app, client=LOOPBACK_PEER)
        response = client.get(f"{path}?lang=zh")
        assert response.status_code == 200, (path, response.text)
