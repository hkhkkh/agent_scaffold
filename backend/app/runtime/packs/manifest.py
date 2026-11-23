"""Capability pack manifests.

A *capability pack* is the unit an optional ability arrives in: its tools, the
Python packages they import, the permissions it asks for, and the secrets its
deployment has to supply. Both the canvas and an exported agent read the same
manifest, which is what keeps "works in the canvas" and "works standalone" from
drifting into two capability lists that only look alike.

Why a manifest instead of just registering more tools
-----------------------------------------------------
`register_tool()` answers "what can this process call?". It cannot answer the
three questions a *deployment* has:

1. **What does this agent actually need?** An exported agent should carry the
   packs its definition names, not every pack the builder happened to install.
2. **What has to be installed on the customer's machine?** That is a list of
   pip requirements, and it belongs next to the code that imports them.
3. **What is this pack allowed to do?** Files, network and databases are
   different kinds of risk; the permission ids in the manifest are what the
   policy layer checks, and what the audit trail records.

The manifest is deliberately data, not behaviour: it is written to
`manifest.yml` at export, so a deployer can read what an agent needs without
reading Python.

Versioning
----------
`api_version` describes the *manifest shape*, not the pack. A future field that
changes meaning must bump it; adding an optional field does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "PACK_API_VERSION",
    "ManifestError",
    "PackDependency",
    "PackManifest",
]

#: Shape of the manifest this runtime understands.
PACK_API_VERSION = "1"

#: Pack names are also YAML keys and command-line words, so keep them boring.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

#: `domain.action`, e.g. `files.write`. Validated here so a typo fails at load
#: rather than at the first policy check, where it would read as "permission
#: denied" and send someone looking at the wrong layer.
_PERMISSION_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

_SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: Characters that end the package name in a pip requirement specifier.
_REQ_NAME_END = re.compile(r"[<>=!~;\[\s@]")


class ManifestError(ValueError):
    """A manifest that cannot be trusted.

    Raised while packs are loaded, never at first use: a pack that silently
    misses one dependency is an agent that starts and then fails in the middle
    of a customer demo.
    """


@dataclass(frozen=True)
class PackDependency:
    """One third-party Python distribution a pack needs.

    ``import_name`` is explicit rather than derived because the two do not always
    agree -- `python-docx` imports as `docx` -- and deriving it from the pip name
    is exactly the kind of cleverness that produces a doctor report saying the
    dependency is missing when it is installed.
    """

    spec: str
    import_name: str = ""
    #: Optional dependencies may be missing; the pack degrades instead of failing.
    optional: bool = False

    def __post_init__(self) -> None:
        spec = (self.spec or "").strip()
        if not spec:
            raise ManifestError("a dependency has an empty spec")
        object.__setattr__(self, "spec", spec)
        if not self.import_name:
            base = _REQ_NAME_END.split(spec, maxsplit=1)[0]
            object.__setattr__(self, "import_name", base.replace("-", "_").lower())
        if not self.import_name.isidentifier():
            raise ManifestError(
                f"dependency '{spec}' needs an explicit import_name; "
                f"'{self.import_name}' is not a Python identifier"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "import_name": self.import_name,
            "optional": self.optional,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "PackDependency":
        if isinstance(value, str):
            return cls(spec=value)
        if isinstance(value, Mapping):
            return cls(
                spec=str(value.get("spec", "")),
                import_name=str(value.get("import_name", "")),
                optional=bool(value.get("optional", False)),
            )
        raise ManifestError(f"dependency must be a string or mapping, got {type(value).__name__}")


@dataclass(frozen=True)
class PackManifest:
    """What one capability pack declares about itself.

    Nothing here executes. The pack's code is in the module that owns the
    manifest; this object is what the registry, the exporter, the policy layer
    and the doctor all agree on.
    """

    name: str
    version: str = "1.0.0"
    title: str = ""
    title_zh: str = ""
    description: str = ""
    description_zh: str = ""
    #: Tools this pack provides, by registry name.
    tools: tuple[str, ...] = ()
    #: Node types this pack provides. Empty for tool-only packs today; the field
    #: exists because a pack is the natural boundary for one, and a manifest that
    #: had to change shape to add nodes would be the wrong shape.
    nodes: tuple[str, ...] = ()
    #: Permission ids this pack's code checks before acting.
    permissions: tuple[str, ...] = ()
    #: Environment variable NAMES a deployment may supply. Never values.
    secrets: tuple[str, ...] = ()
    #: Third-party distributions the pack imports.
    dependencies: tuple[PackDependency, ...] = ()
    #: Defaults the pack would like in Settings, as dotted names to values. The
    #: platform merges them into the environment *before* Settings is built;
    #: standalone agents do the same from their manifest.
    settings: Mapping[str, Any] = field(default_factory=dict)
    api_version: str = PACK_API_VERSION
    notes: str = ""

    def __post_init__(self) -> None:
        problems = self.problems()
        if problems:
            raise ManifestError(
                f"manifest '{self.name}' is invalid: " + "; ".join(problems)
            )

    # ------------------------------------------------------------------ checks
    def problems(self) -> list[str]:
        """Everything that is wrong with this manifest. Empty means usable."""
        problems: list[str] = []
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            problems.append(
                f"name {self.name!r} must match {_NAME_RE.pattern} (lowercase, underscores)"
            )
        for field_name, values in (
            ("tools", self.tools),
            ("nodes", self.nodes),
            ("permissions", self.permissions),
            ("secrets", self.secrets),
        ):
            seen: set[str] = set()
            for value in values:
                if not isinstance(value, str) or not value:
                    problems.append(f"{field_name} contains a non-name entry {value!r}")
                    continue
                if value in seen:
                    problems.append(f"{field_name} lists {value!r} twice")
                seen.add(value)
        for permission in self.permissions:
            if not _PERMISSION_RE.match(permission):
                problems.append(
                    f"permission {permission!r} must look like 'domain.action'"
                )
        for secret in self.secrets:
            if not _SECRET_RE.match(secret):
                problems.append(f"secret {secret!r} must be an UPPER_SNAKE_CASE env name")
        if self.api_version != PACK_API_VERSION:
            problems.append(
                f"api_version {self.api_version!r} is not understood by this runtime "
                f"({PACK_API_VERSION})"
            )
        return problems

    # ------------------------------------------------------------- serialising
    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "name": self.name,
            "version": self.version,
            "title": self.title,
            "title_zh": self.title_zh,
            "description": self.description,
            "description_zh": self.description_zh,
            "tools": list(self.tools),
            "nodes": list(self.nodes),
            "permissions": list(self.permissions),
            "secrets": list(self.secrets),
            "dependencies": [dependency.to_dict() for dependency in self.dependencies],
            "settings": dict(self.settings),
            "notes": self.notes,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "PackManifest":
        """Build a manifest from data (used by an exported agent's manifest.yml)."""
        if not isinstance(payload, Mapping):
            raise ManifestError("manifest payload must be a mapping")
        dependencies = tuple(
            PackDependency.from_mapping(value)
            for value in (payload.get("dependencies") or ())
        )
        return cls(
            name=str(payload.get("name", "")),
            version=str(payload.get("version", "1.0.0")),
            title=str(payload.get("title", "")),
            title_zh=str(payload.get("title_zh", "")),
            description=str(payload.get("description", "")),
            description_zh=str(payload.get("description_zh", "")),
            tools=tuple(str(value) for value in (payload.get("tools") or ())),
            nodes=tuple(str(value) for value in (payload.get("nodes") or ())),
            permissions=tuple(str(value) for value in (payload.get("permissions") or ())),
            secrets=tuple(str(value) for value in (payload.get("secrets") or ())),
            dependencies=dependencies,
            settings=dict(payload.get("settings") or {}),
            api_version=str(payload.get("api_version", PACK_API_VERSION)),
            notes=str(payload.get("notes", "")),
        )
