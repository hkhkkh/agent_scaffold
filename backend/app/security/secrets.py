"""Credential lookup — where a secret comes from, as opposed to what it is.

Why this module exists
----------------------

Every credential in the product is read straight off ``Settings``, which means
every credential comes from ``.env``. That is fine for one deployment on one
machine and wrong for the two things the roadmap actually needs:

* **An exported agent is its own deployment** (Phase 6). Its credentials have to
  travel with it as data. A package that reads ``.env`` is not self-contained —
  it is a package that expects the destination to have been configured the same
  way the origin was.
* **A production deployment uses a secret manager.** Vault, AWS Secrets
  Manager, and Kubernetes secrets all want to be asked for a value by name,
  lazily, with no copy of it in the process's own configuration.

Neither is a rename of ``settings.llm_api_key``. Both need a seam where "where
does this secret come from" is a deployment decision.

The shape follows the other four capability modules (:mod:`app.runtime.egress`,
:mod:`app.runtime.runner`, :mod:`app.tools.registry`, :mod:`app.runtime.ai.gateway`) —
ABC, a default implementation, a process-wide injection point, and a builder
that reads configuration. Five modules sharing one shape is a convention.

What this module deliberately does not do
-----------------------------------------

**It does not decide whether a credential may be used.** That rule — a
credential does not follow a redirect to a different endpoint — lives in
:func:`app.runtime.ai.provider.resolve_llm_config` and stays there, because it is about
*two* values (the endpoint and the key) and one of them is not a secret. Moving
it here would mean passing endpoint information into a module that has no
business knowing about endpoints.

**It does not mask secrets in output.** :func:`app.runtime.serialize.redact`
does that by key name, and it already works. Masking by *value* would be
stronger — it would catch a secret stored under an innocent key — but
``redact`` runs on every event and every step record, and scanning arbitrary
payloads for every known secret on that path is a cost with no measured
benefit. Recorded here as a deliberate choice rather than an oversight.

A note on node-level overrides
------------------------------

A node may carry its own ``api_key`` in its config. That value is written to
``data/workflows/<id>.json`` in plaintext, because the workflow export is
git-friendly by design and the node config is part of it. The ``redact`` pass
hides it in the UI and in run events, but not on disk. That is a pre-existing
property of the export format, not something this module introduces — and
removing it would mean deciding that workflow exports may not contain
credentials, which is a product decision rather than a security fix. Called out
here so it is a known trade-off instead of a surprise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from app.settings import Settings, get_settings

__all__ = [
    "SECRET_LLM_API_KEY",
    "MappingSecretProvider",
    "SecretError",
    "SecretProvider",
    "SettingsSecretProvider",
    "build_secret_provider",
    "get_secret_provider",
    "set_secret_provider",
]

#: Names are dotted so a manager backend can map them onto its own hierarchy
#: (``secret/agent-scaffold/llm.api_key``). Only the names actually in use are
#: declared — a registry of secrets that do not exist yet is a list someone has
#: to keep accurate for no benefit.
SECRET_LLM_API_KEY = "llm.api_key"
#: Kept separate from the chat key so embeddings can point at a different vendor
#: — or at none, leaving memory search literal — without disturbing the chat
#: model. `app/memory/store.py` falls back to the chat key when this one is unset,
#: because in the common case both come from the same provider.
SECRET_EMBEDDING_API_KEY = "embedding.api_key"


class SecretError(LookupError):
    """Raised by :meth:`SecretProvider.require` when a secret is not configured.

    ``LookupError`` rather than ``ValueError``: the failure is "there is no such
    entry", which is what a lookup error means, and it keeps a caller's existing
    ``except KeyError`` habits working.
    """


class SecretProvider:
    """Looks up secrets by name.

    Implementations must:

    * return ``None`` for a name that is **not configured**, and never raise for
      it — "absent" is a normal state (a machine with no API key runs the mock
      model) and a caller that has to catch an exception to detect it will
      eventually catch the wrong one;
    * treat an **empty string as absent**, because that is how ``.env``
      expresses "not set" and how a secret manager's empty value would arrive.
      Two ways to say "no secret" would be one too many;
    * expose *whether* a secret exists separately from its value, so an operator
      can log the posture without logging the secret.
    """

    #: Where the values come from, for a log line and for callers that need to
    #: know whether writing ``settings.llm_api_key`` would have any effect.
    #: A short identifier rather than a class name, so the question "is this
    #: deployment settings-backed" does not depend on a class existing under a
    #: particular name.
    source: str = "settings"

    def get(self, name: str) -> str | None:
        raise NotImplementedError

    def has(self, name: str) -> bool:
        value = self.get(name)
        return bool(value and value.strip())

    def require(self, name: str) -> str:
        """The value, or :class:`SecretError` naming what is missing.

        Names the *name*, never a near-miss of the value — an error message is
        a log line, and a log line is not where a credential should appear.
        """
        value = self.get(name)
        if not value or not value.strip():
            raise SecretError(
                f"Secret '{name}' is not configured. "
                "Set it in .env, or configure a secrets file / secret manager."
            )
        return value.strip()

    def describe(self) -> dict[str, bool]:
        """Which known secrets are present. **Values are never included.**

        This is the method that makes an operator's life possible: a startup log
        line saying "llm.api_key: present" answers "did my deployment pick up the
        credential" without putting it anywhere it can be copied from.
        """
        return {name: self.has(name) for name in KNOWN_SECRETS}


#: Every name the product asks for. Kept next to the interface so
#: ``describe()`` cannot drift away from what is actually used.
KNOWN_SECRETS: tuple[str, ...] = (SECRET_LLM_API_KEY, SECRET_EMBEDDING_API_KEY)


@dataclass
class SettingsSecretProvider(SecretProvider):
    """Default: whatever ``.env`` and the environment produced.

    Thin on purpose — the point is not to replace ``Settings`` but to put one
    object between a caller and it, so "where credentials come from" stops being
    a hard-coded attribute access.
    """

    settings: Settings | None = None

    source: str = "settings"

    def get(self, name: str) -> str | None:
        settings = self.settings or get_settings()
        if name == SECRET_LLM_API_KEY:
            return settings.llm_api_key or None
        if name == SECRET_EMBEDDING_API_KEY:
            return settings.embedding_api_key or None
        return None


@dataclass
class MappingSecretProvider(SecretProvider):
    """Secrets held in memory, keyed by name.

    Two real uses, and neither is a test fixture:

    * **An exported agent's bundle.** Phase 6 ships an agent with its own
      credentials so that the destination does not have to be configured to
      match the origin. Loading that bundle gives one of these.
    * **A deployment that injects credentials some other way** — a supervisor
      that passes them in, a mounted file read at startup.

    ``extra`` exists because ``describe()`` should report what an operator
    actually configured, not only the names the product happens to know today.
    A secret in the bundle that nothing reads yet is still worth seeing in the
    posture line.
    """

    values: Mapping[str, str] = field(default_factory=dict)
    extra: frozenset[str] = frozenset()

    source: str = "memory"

    def get(self, name: str) -> str | None:
        return self.values.get(name) or None

    def describe(self) -> dict[str, bool]:
        described = {name: self.has(name) for name in KNOWN_SECRETS}
        for name in sorted(self.extra):
            described.setdefault(name, self.has(name))
        return described


def load_secrets_file(path: Path) -> MappingSecretProvider:
    """Read a JSON object of ``name -> value``. Fails loudly, never silently.

    A deployment that pointed at a secrets file and got zero secrets out of it
    would fall back to the mock model and look like a *working* system — the
    worst possible outcome, because nothing is obviously wrong. So a missing or
    malformed file raises instead of degrading.

    Returns an empty provider for a well-formed empty object, which is a
    legitimate way to say "this bundle carries no credentials of its own".
    """
    if not path.exists():
        raise SecretError(f"Secrets file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SecretError(f"Secrets file is not valid JSON ({path}): {exc}") from exc
    if not isinstance(raw, dict):
        raise SecretError(
            f"Secrets file must contain a JSON object of name -> value ({path})."
        )

    values: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        values[str(key)] = str(value)
    return MappingSecretProvider(
        values=values,
        extra=frozenset(set(values) - set(KNOWN_SECRETS)),
        source="file",
    )


def build_secret_provider(settings: Settings | None = None) -> SecretProvider:
    """Construct the provider from configuration.

    A configured ``SECRETS_FILE`` wins over ``.env``. That ordering is the point
    of the feature: an exported agent must be able to carry its own credentials
    without the destination's ``.env`` overriding them.
    """
    settings = settings or get_settings()
    configured = getattr(settings, "secrets_file", None)
    if configured:
        return load_secrets_file(Path(configured))
    return SettingsSecretProvider(settings=settings)


#: Process-wide provider. Swapped in tests, and the seam where a Vault or
#: Secrets Manager backend plugs in without any caller changing.
_provider: SecretProvider | None = None


def get_secret_provider() -> SecretProvider:
    global _provider
    if _provider is None:
        _provider = build_secret_provider()
    return _provider


def set_secret_provider(provider: SecretProvider | None) -> None:
    """Override the process provider. Passing ``None`` restores the configured one."""
    global _provider
    _provider = provider
