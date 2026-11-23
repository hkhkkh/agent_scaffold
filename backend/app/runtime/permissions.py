"""Permission ids and the policy that answers allow / deny / needs-a-human.

Why a policy object, when the checks are one line each
------------------------------------------------------
The *checks* are identical everywhere: a pack asks "may I write this file?", and
the same call site runs in the canvas, in a test and in an exported agent. The
*answer* is not: a laptop owner can write files in their own workspace, a
customer's server may narrow what an agent may touch, and the public demo may
refuse the capability entirely. Separating the two is what keeps "runs in the
canvas" and "runs standalone" from growing different security models.

Every pack manifest lists the permissions its code checks. The registry can
therefore say, before anything runs, what a pack is allowed to do.

Approval today
--------------
:attr:`PolicyDecision.requires_approval` is a first-class answer, but there is no
human-approval workbench yet. Until there is, :meth:`PermissionPolicy.check`
turns "needs approval" into a refusal that says exactly that -- failing closed
instead of pretending an approval happened. The configuration
(``PACK_PERMISSION_APPROVAL``) is already read here, so wiring the workbench in
later is a UI task, not a policy rewrite.

The default is permissive **on purpose**: refusing by default would make the
laptop experience unusable and push people towards disabling the checks
altogether. What keeps the default honest is that (a) the public demo is
deny-all, (b) writes are audited, and (c) an exported agent can narrow the set
in its manifest without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from app.settings import get_settings

__all__ = [
    "ALL_PERMISSIONS",
    "DB_READ",
    "DB_WRITE",
    "FILES_DELETE",
    "FILES_LIST",
    "FILES_READ",
    "FILES_WRITE",
    "NET_HTTP",
    "PYTHON_EVAL",
    "WEB_SEARCH",
    "AllowAllPolicy",
    "AllowlistPolicy",
    "DenyAllPolicy",
    "PermissionDenied",
    "PermissionPolicy",
    "PolicyDecision",
    "build_permission_policy",
    "check_permission",
    "get_permission_policy",
    "set_permission_policy",
]

# ---------------------------------------------------------------------------
# Permission ids: `domain.action`
# ---------------------------------------------------------------------------

FILES_READ = "files.read"
FILES_LIST = "files.list"
FILES_WRITE = "files.write"
FILES_DELETE = "files.delete"
NET_HTTP = "net.http"
WEB_SEARCH = "web.search"
DB_READ = "db.read"
DB_WRITE = "db.write"
PYTHON_EVAL = "python.eval"

#: Everything this runtime understands. A manifest naming anything else is a
#: mistake (the registry reports it) rather than a permission that silently
#: never matches a check.
ALL_PERMISSIONS: frozenset[str] = frozenset(
    {
        FILES_READ,
        FILES_LIST,
        FILES_WRITE,
        FILES_DELETE,
        NET_HTTP,
        WEB_SEARCH,
        DB_READ,
        DB_WRITE,
        PYTHON_EVAL,
    }
)


class PermissionDenied(PermissionError):
    """A policy refusal.

    Carries ``permission`` and ``requires_approval`` so a caller can render
    "this deployment does not allow file writes" and "a human has to approve
    this" as different messages -- they need different fixes.
    """

    def __init__(
        self,
        permission: str,
        *,
        reason: str = "",
        requires_approval: bool = False,
    ) -> None:
        text = reason or f"permission '{permission}' was refused"
        super().__init__(text)
        self.permission = permission
        self.reason = text
        self.requires_approval = requires_approval


@dataclass(frozen=True)
class PolicyDecision:
    """The answer to one permission question."""

    allowed: bool
    requires_approval: bool = False
    reason: str = ""

    @classmethod
    def allow(cls, reason: str = "") -> "PolicyDecision":
        return cls(allowed=True, reason=reason)

    @classmethod
    def deny(cls, reason: str = "") -> "PolicyDecision":
        return cls(allowed=False, reason=reason)

    @classmethod
    def approval(cls, reason: str = "") -> "PolicyDecision":
        return cls(allowed=True, requires_approval=True, reason=reason)


class PermissionPolicy:
    """Decides one permission question at a time."""

    def decide(
        self,
        permission: str,
        *,
        tool: str = "",
        context: Mapping[str, Any] | None = None,
    ) -> PolicyDecision:
        raise NotImplementedError

    def check(
        self,
        permission: str,
        *,
        tool: str = "",
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """Raise :class:`PermissionDenied` unless the action may proceed now."""
        decision = self.decide(permission, tool=tool, context=context)
        if decision.allowed and not decision.requires_approval:
            return
        if decision.allowed and decision.requires_approval:
            reason = decision.reason or f"'{permission}' requires human approval"
            raise PermissionDenied(
                permission,
                reason=reason + " (no approval workbench is configured yet, so this fails closed)",
                requires_approval=True,
            )
        raise PermissionDenied(permission, reason=decision.reason)


class AllowAllPolicy(PermissionPolicy):
    """Permit every declared permission. The laptop and default-export answer."""

    def decide(self, permission, *, tool="", context=None):  # noqa: D102
        return PolicyDecision.allow()


class DenyAllPolicy(PermissionPolicy):
    """Refuse everything. The public-demo answer."""

    def decide(self, permission, *, tool="", context=None):  # noqa: D102
        return PolicyDecision.deny(
            "this deployment does not allow capability packs to act"
        )


class AllowlistPolicy(PermissionPolicy):
    """Permit only what the deployment names; optionally require approval first.

    This is the shape a customer deployment uses: "this agent may read and write
    files in its workspace, may call our API, but may not touch the database".
    """

    def __init__(
        self,
        allow: Iterable[str],
        *,
        approval_required: Iterable[str] = (),
    ) -> None:
        self.allow = frozenset(allow)
        self.approval_required = frozenset(approval_required)

    def decide(
        self,
        permission: str,
        *,
        tool: str = "",
        context: Mapping[str, Any] | None = None,
    ) -> PolicyDecision:
        if permission not in self.allow:
            return PolicyDecision.deny(
                f"permission '{permission}' is not in this deployment's allow list"
            )
        if permission in self.approval_required:
            return PolicyDecision.approval(
                f"permission '{permission}' requires human approval"
            )
        return PolicyDecision.allow()


def _parse_names(raw: str) -> frozenset[str]:
    return frozenset(
        part.strip() for part in (raw or "").replace(";", ",").split(",") if part.strip()
    )


def build_permission_policy(settings: Any = None) -> PermissionPolicy:
    """Construct the policy from configuration.

    * ``PACK_PERMISSIONS`` names a comma-separated allow list; unset means "all".
    * ``PACK_PERMISSION_APPROVAL`` names permissions that need a human first.
    * ``PUBLIC_DEMO=true`` is deny-all regardless of the other two, because a
      public visitor must not be one env typo away from a file write.
    """
    settings = settings or get_settings()
    if getattr(settings, "public_demo", False):
        return DenyAllPolicy()
    allow = _parse_names(os.environ.get("PACK_PERMISSIONS", ""))
    approval = _parse_names(os.environ.get("PACK_PERMISSION_APPROVAL", ""))
    if not allow and not approval:
        return AllowAllPolicy()
    # An approval list without an allow list means "everything, but ask first for
    # these" -- expressing it as an allowlist of all known permissions keeps the
    # two lists from needing different code paths.
    if not allow:
        allow = ALL_PERMISSIONS
    return AllowlistPolicy(allow, approval_required=approval)


#: Process-wide policy. Swapped in tests; the seam a deployment narrows.
_policy: PermissionPolicy | None = None


def get_permission_policy() -> PermissionPolicy:
    global _policy
    if _policy is None:
        _policy = build_permission_policy()
    return _policy


def set_permission_policy(policy: PermissionPolicy | None) -> None:
    """Override the process policy. ``None`` restores the configured one."""
    global _policy
    _policy = policy


def check_permission(
    permission: str,
    *,
    tool: str = "",
    context: Mapping[str, Any] | None = None,
) -> None:
    """Check one permission with the process policy. Raises on refusal."""
    get_permission_policy().check(permission, tool=tool, context=context)
