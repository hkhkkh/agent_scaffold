"""Phase 1 capability layer — the secret provider.

What these tests are actually defending
---------------------------------------

Credentials are the one part of the capability layer where a mistake is not a
bug, it is an incident. So the assertions here are weighted toward the negative
direction:

* a **secret never reaches a log line or a ``describe()`` payload** — the
  posture view exists precisely so an operator does not have to print the value,
  and a view that leaked one would be worse than no view at all;
* a **configured secret source cannot be overridden from a call site** — the
  explicit ``settings`` argument to ``resolve_llm_config`` honours the passed
  object only when the deployment is settings-backed, because otherwise it would
  be a way to substitute someone else's credential;
* a **malformed secrets file fails loudly** rather than degrading to "no
  secrets", because a deployment that silently fell back to the mock model
  looks like a working system.

The one positive assertion that matters most is the Phase 0 credential rule
still holding: a node pointing at a different endpoint still does not receive
the operator's key. That test lives in ``test_security.py`` and was deliberately
left untouched — this file adds a companion that exercises the same rule with a
secrets file in play, since that is the new way the credential can arrive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.security.secrets import (
    SECRET_LLM_API_KEY,
    MappingSecretProvider,
    SecretError,
    SecretProvider,
    SettingsSecretProvider,
    build_secret_provider,
    get_secret_provider,
    load_secrets_file,
    set_secret_provider,
)
from app.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _restore_provider():
    """The provider is process-wide, so every test here must restore it."""
    yield
    set_secret_provider(None)
    get_settings.cache_clear()


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class TestTheInterfaceIsCoherent:
    def test_both_implementations_satisfy_it(self):
        for provider in (SettingsSecretProvider(), MappingSecretProvider()):
            assert isinstance(provider, SecretProvider)
            assert isinstance(provider.describe(), dict)

    def test_absent_is_none_and_does_not_raise(self):
        """`None` for absent, not an exception.

        "Not configured" is a normal state — a machine with no API key runs the
        mock model — and a caller forced to catch an exception to detect it will
        eventually catch the wrong one.
        """
        assert MappingSecretProvider().get(SECRET_LLM_API_KEY) is None
        assert SettingsSecretProvider(settings=_settings()).get(SECRET_LLM_API_KEY) is None

    def test_an_empty_string_counts_as_absent(self):
        """`.env` expresses "not set" as an empty string; a secret manager may too.

        Two ways to say "no secret" would be one too many, and the empty string
        would otherwise travel all the way into an `Authorization: Bearer `
        header.

        Note this holds at the `get()` level already (`'' or None` is `None`), so
        it does *not* exercise `has()`'s strip. The whitespace test below is the
        one that does — established by the reverse validation reporting this
        case as unfalsifiable.
        """
        provider = MappingSecretProvider(values={SECRET_LLM_API_KEY: ""})

        assert provider.get(SECRET_LLM_API_KEY) is None
        assert provider.has(SECRET_LLM_API_KEY) is False

    def test_whitespace_also_counts_as_absent(self):
        """The case that actually needs `has()`'s strip.

        An all-whitespace value is truthy, so without the strip `has()` would
        report a secret as present and `Authorization: Bearer    ` would be
        sent.
        """
        provider = MappingSecretProvider(values={SECRET_LLM_API_KEY: "   "})

        assert provider.get(SECRET_LLM_API_KEY) == "   "  # truthy on its own
        assert provider.has(SECRET_LLM_API_KEY) is False

    def test_require_names_what_is_missing_and_nothing_else(self):
        """The error must not hint at a value — it lands in a log file."""
        with pytest.raises(SecretError, match=SECRET_LLM_API_KEY):
            MappingSecretProvider().require(SECRET_LLM_API_KEY)

    def test_require_returns_the_stripped_value(self):
        provider = MappingSecretProvider(values={SECRET_LLM_API_KEY: "  sk-x  "})

        assert provider.require(SECRET_LLM_API_KEY) == "sk-x"

    def test_source_identifies_where_values_come_from(self):
        """`source` is what lets a caller ask "is this settings-backed?".

        A short identifier rather than a class name: the question should not
        depend on a class existing under a particular name.
        """
        assert SettingsSecretProvider().source == "settings"
        assert MappingSecretProvider().source == "memory"


class TestDescribeNeverLeaksAValue:
    """The whole reason `describe()` exists is so a value need not be printed.

    Parametrized over **both** implementations on purpose. Only the mapping one
    was covered at first, and the base-class implementation — which
    `SettingsSecretProvider` actually uses — was left unguarded. The reverse
    validation reported "not caught" for exactly that reason, because the
    mutation was in the base class and the test never reached it.
    """

    @staticmethod
    def _providers() -> list[SecretProvider]:
        return [
            SettingsSecretProvider(settings=_settings(llm_api_key="sk-super-secret")),
            MappingSecretProvider(values={SECRET_LLM_API_KEY: "sk-super-secret"}),
        ]

    def test_describe_reports_presence_not_content(self):
        for provider in self._providers():
            described = provider.describe()

            assert described[SECRET_LLM_API_KEY] is True, type(provider).__name__
            assert "sk-super-secret" not in json.dumps(described)
            # `is` rather than truthiness: a non-empty string is truthy too, so
            # `assert described[...]` would pass while the value leaked.
            assert all(value is True or value is False for value in described.values())

    def test_describe_covers_every_known_secret(self, monkeypatch):
        """A coverage guarantee needs more than one name to mean anything.

        `KNOWN_SECRETS` holds a single entry today, so "returns only the first"
        and "returns all of them" produce the same dict and no test could tell
        them apart. Adding a second name — which is what will happen as soon as
        another credential exists — is what makes the loop load-bearing.
        """
        import app.security.secrets as module

        monkeypatch.setattr(
            module, "KNOWN_SECRETS", (*module.KNOWN_SECRETS, "future.second_key")
        )

        described = SettingsSecretProvider(settings=_settings()).describe()

        assert len(described) >= 2
        for name in module.KNOWN_SECRETS:
            assert name in described

    def test_describe_includes_names_the_product_does_not_read_yet(self):
        """A configured-but-unused secret is worth seeing in the posture line.

        It usually means a typo, or a bundle built for a newer version.
        """
        provider = MappingSecretProvider(
            values={SECRET_LLM_API_KEY: "sk-x", "some.future.key": "y"},
            extra=frozenset({"some.future.key"}),
        )

        described = provider.describe()

        assert described["some.future.key"] is True
        assert "sk-x" not in json.dumps(described)

    def test_describe_on_an_empty_provider_is_all_false(self):
        described = SettingsSecretProvider(settings=_settings()).describe()

        assert described and not any(described.values())


# ---------------------------------------------------------------------------
# Loading a bundle
# ---------------------------------------------------------------------------


class TestSecretsFile:
    def test_reads_a_json_object(self, tmp_path: Path):
        path = tmp_path / "secrets.json"
        path.write_text(
            json.dumps({SECRET_LLM_API_KEY: "sk-from-file"}), encoding="utf-8"
        )

        provider = load_secrets_file(path)

        assert provider.get(SECRET_LLM_API_KEY) == "sk-from-file"
        assert provider.source == "file"

    def test_a_missing_file_raises_rather_than_degrading(self, tmp_path: Path):
        """Failing loudly is the point.

        A deployment that pointed at a secrets file, got nothing, and fell back
        to the mock model would look like a *working* system. That is the worst
        outcome available, because nothing is obviously wrong.
        """
        with pytest.raises(SecretError, match="not found"):
            load_secrets_file(tmp_path / "absent.json")

    def test_malformed_json_raises(self, tmp_path: Path):
        path = tmp_path / "secrets.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(SecretError, match="not valid JSON"):
            load_secrets_file(path)

    def test_a_non_object_raises(self, tmp_path: Path):
        path = tmp_path / "secrets.json"
        path.write_text('["sk-x"]', encoding="utf-8")

        with pytest.raises(SecretError, match="JSON object"):
            load_secrets_file(path)

    def test_an_empty_object_is_legitimate(self, tmp_path: Path):
        """A bundle may carry no credentials of its own."""
        path = tmp_path / "secrets.json"
        path.write_text("{}", encoding="utf-8")

        provider = load_secrets_file(path)

        assert provider.get(SECRET_LLM_API_KEY) is None

    def test_null_values_are_skipped(self, tmp_path: Path):
        path = tmp_path / "secrets.json"
        path.write_text(json.dumps({SECRET_LLM_API_KEY: None}), encoding="utf-8")

        assert load_secrets_file(path).has(SECRET_LLM_API_KEY) is False

    def test_unknown_names_are_reported_as_extra(self, tmp_path: Path):
        path = tmp_path / "secrets.json"
        path.write_text(
            json.dumps({SECRET_LLM_API_KEY: "sk-x", "typo.key": "y"}), encoding="utf-8"
        )

        provider = load_secrets_file(path)

        assert provider.describe()["typo.key"] is True


class TestBuildingFromConfiguration:
    def test_no_configuration_gives_the_settings_provider(self):
        assert isinstance(build_secret_provider(_settings()), SettingsSecretProvider)

    def test_a_configured_file_wins_over_settings(self, tmp_path: Path):
        """That ordering is the feature: an exported agent carries its own key."""
        path = tmp_path / "secrets.json"
        path.write_text(
            json.dumps({SECRET_LLM_API_KEY: "sk-from-file"}), encoding="utf-8"
        )

        provider = build_secret_provider(_settings(secrets_file=str(path)))

        assert provider.source == "file"
        assert provider.get(SECRET_LLM_API_KEY) == "sk-from-file"

    def test_a_configured_but_broken_file_stops_startup(self, tmp_path: Path):
        with pytest.raises(SecretError):
            build_secret_provider(_settings(secrets_file=str(tmp_path / "nope.json")))


# ---------------------------------------------------------------------------
# The seam is live
# ---------------------------------------------------------------------------


class TestTheCredentialComesFromTheProvider:
    def test_the_settings_provider_is_the_default(self):
        assert get_secret_provider().source == "settings"

    def test_the_getter_is_lazy_and_the_setter_restores(self):
        assert isinstance(get_secret_provider(), SecretProvider)

        replacement = MappingSecretProvider(values={SECRET_LLM_API_KEY: "sk-x"})
        set_secret_provider(replacement)
        assert get_secret_provider() is replacement

        set_secret_provider(None)
        assert get_secret_provider() is not replacement

    def test_resolve_llm_config_reads_the_installed_provider(self):
        from app.runtime.ai.provider import resolve_llm_config

        set_secret_provider(
            MappingSecretProvider(values={SECRET_LLM_API_KEY: "sk-from-provider"})
        )
        settings = _settings(
            llm_api_key="sk-from-env", llm_base_url="https://api.deepseek.com/v1"
        )

        config = resolve_llm_config(None, settings)

        assert config["api_key"] == "sk-from-provider"

    def test_is_mock_configured_follows_the_provider(self):
        """Otherwise the UI reports the mock model while a real one is in use."""
        from app.runtime.ai.provider import is_mock_configured

        settings = _settings(llm_api_key="", llm_base_url="https://api.openai.com/v1")
        assert is_mock_configured(settings) is True

        set_secret_provider(
            MappingSecretProvider(values={SECRET_LLM_API_KEY: "sk-from-provider"})
        )
        assert is_mock_configured(settings) is False

    def test_a_settings_backed_deployment_still_honours_the_passed_settings(self):
        """The explicit `settings` argument must keep working.

        Phase 0's credential tests pass a Settings object and expect the key to
        come from it. Reading only the process-wide provider would quietly
        change that contract — and those tests are the regression record for a
        real vulnerability, so weakening them is not an option.
        """
        from app.runtime.ai.provider import resolve_llm_config

        settings = _settings(
            llm_api_key="sk-explicit", llm_base_url="https://api.deepseek.com/v1"
        )

        assert resolve_llm_config(None, settings)["api_key"] == "sk-explicit"

    def test_a_configured_source_cannot_be_overridden_from_a_call_site(self):
        """The direction that would be a hole, asserted explicitly.

        If a secrets file is configured and a caller could still dictate the
        credential by passing a Settings object, then "where does this
        deployment keep its key" would have two answers and the weaker one would
        win.
        """
        from app.runtime.ai.provider import resolve_llm_config

        set_secret_provider(
            MappingSecretProvider(values={SECRET_LLM_API_KEY: "sk-from-file"}, source="file")
        )
        settings = _settings(
            llm_api_key="sk-injected", llm_base_url="https://api.deepseek.com/v1"
        )

        config = resolve_llm_config(None, settings)

        assert config["api_key"] == "sk-from-file"


class TestCredentialsStillDoNotFollowARedirectWithASecretsFile:
    """Phase 0's rule, re-checked through the new path.

    The Phase 0 tests cover the settings-backed case. A secrets file is a new
    way for the credential to arrive, so the rule needs to be shown to hold
    there too — otherwise the abstraction would have introduced the leak it was
    built to make configurable.
    """

    def test_a_different_endpoint_still_gets_no_key(self):
        from app.runtime.ai.provider import resolve_llm_config

        set_secret_provider(
            MappingSecretProvider(values={SECRET_LLM_API_KEY: "sk-operator-secret"}, source="file")
        )
        settings = _settings(llm_base_url="https://api.deepseek.com/v1")

        config = resolve_llm_config(
            {"base_url": "https://attacker.example/v1"}, settings
        )

        assert config["base_url"] == "https://attacker.example/v1"
        assert config["api_key"] == ""

    def test_the_same_endpoint_still_gets_the_key(self):
        from app.runtime.ai.provider import resolve_llm_config

        set_secret_provider(
            MappingSecretProvider(values={SECRET_LLM_API_KEY: "sk-operator-secret"}, source="file")
        )
        settings = _settings(llm_base_url="https://api.deepseek.com/v1")

        config = resolve_llm_config({"base_url": settings.llm_base_url}, settings)

        assert config["api_key"] == "sk-operator-secret"


class TestTheStartupPostureLine:
    def test_it_reports_presence_and_never_the_value(self, caplog):
        """Asserted on the real application, because that is where it runs."""
        import logging

        from fastapi.testclient import TestClient

        from app.main import app

        set_secret_provider(
            MappingSecretProvider(values={SECRET_LLM_API_KEY: "sk-must-not-appear"})
        )

        with caplog.at_level(logging.INFO, logger="app"):
            with TestClient(app, client=("127.0.0.1", 51234)):
                pass

        assert "sk-must-not-appear" not in caplog.text
        assert f"{SECRET_LLM_API_KEY}=present" in caplog.text

    def test_no_warning_about_a_missing_key_when_one_is_configured(self, caplog):
        """A false "no API key" warning at a configured deployment is alarming."""
        import logging

        from fastapi.testclient import TestClient

        from app.main import app

        set_secret_provider(
            MappingSecretProvider(values={SECRET_LLM_API_KEY: "sk-configured"})
        )

        with caplog.at_level(logging.WARNING, logger="app"):
            with TestClient(app, client=("127.0.0.1", 51234)):
                pass

        assert "No LLM API key configured" not in caplog.text


class TestTheSettingsEndpointRefusesASilentNoOp:
    """`PUT /api/settings/llm` writes memory; a secrets file would ignore it."""

    def test_the_override_is_rejected_when_a_secrets_source_is_active(self):
        from fastapi.testclient import TestClient

        from app.main import app

        set_secret_provider(
            MappingSecretProvider(values={SECRET_LLM_API_KEY: "sk-file"}, source="file")
        )

        with TestClient(app, client=("127.0.0.1", 51234)) as client:
            response = client.put(
                "/api/settings/llm",
                json={
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-flash",
                    "api_key": "sk-typed-into-the-ui",
                    "temperature": 0.0,
                },
            )

        assert response.status_code == 409, response.text
        assert "secrets source" in response.json()["detail"]

    def test_the_override_still_works_for_a_settings_backed_deployment(self):
        """The refusal must not break the ordinary case it was carved out of."""
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app, client=("127.0.0.1", 51234)) as client:
            response = client.put(
                "/api/settings/llm",
                json={
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-flash",
                    "api_key": "sk-typed-into-the-ui",
                    "temperature": 0.0,
                },
            )

        assert response.status_code == 200, response.text
        from app.runtime.ai.provider import resolve_llm_config

        assert resolve_llm_config(None, get_settings())["api_key"] == "sk-typed-into-the-ui"

    def test_the_response_never_contains_the_key(self):
        """The UI only needs to know one exists."""
        from fastapi.testclient import TestClient

        from app.main import app

        set_secret_provider(
            MappingSecretProvider(values={SECRET_LLM_API_KEY: "sk-file"}, source="file")
        )

        with TestClient(app, client=("127.0.0.1", 51234)) as client:
            body = client.get("/api/settings").json()

        assert body["llm"]["api_key"] == "***"
        assert "sk-file" not in json.dumps(body)
