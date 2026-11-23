"""Outbound request policy — the one place that decides what a workflow may reach.

Why this module exists
----------------------

The `http_request` tool takes an arbitrary URL, and that URL can come from a
model, from a user's workflow, or from data flowing through the graph. Before
this module existed, the implementation was a two-line ``httpx.AsyncClient``
call: no scheme check, no address check, ``follow_redirects=True``, and an
unbounded body read. A workflow could therefore reach the cloud metadata
endpoint at ``169.254.169.254``, scan the LAN, or bounce through a redirect to
do either.

The fix is not "add a blocklist to the tool". It is to make *every* outbound
request pass through one policy object, so that:

* adding a new network-calling node cannot accidentally skip the check;
* the policy can be swapped for a stricter one (an allowlist, a proxy, an
  audit sink) without touching node code.

That second point is why the shape is an interface rather than a function. This
is the first member of the capability layer described in
``docs/reference/backend-layers.md#l9``: nodes depend on the interface, and the
platform decides which implementation they get.

What it does and does not defend against
---------------------------------------

It resolves the hostname and inspects **every** address it resolves to, then
re-checks on each redirect hop, so a name that points at a private address is
refused even if the string looks public.

It does **not** defeat DNS rebinding between the check and the connection —
closing that requires pinning the resolved IP and connecting to it directly,
which breaks TLS SNI and virtual hosting. That is a documented limitation, not
an oversight; the honest mitigation for a hostile-DNS threat model is to run
behind an egress proxy with its own resolution.

One more thing worth stating plainly, because it looks alarming and is not:
``httpx`` honours ``HTTP_PROXY``/``HTTPS_PROXY`` by default (``trust_env=True``),
so an outbound request may leave through the operator's proxy. That is *not* a
bypass — this policy runs on the target URL before any connection is made, so a
blocked destination stays blocked whatever route it would have taken. It is
called out because "the request went somewhere I did not expect" and "the request
went somewhere I did not authorise" are different problems, and only the second
one is a security issue.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.settings import Settings, get_settings

#: Schemes that can carry a request. Anything else (`file:`, `gopher:`, `data:`)
#: is refused outright rather than analysed.
ALLOWED_SCHEMES = frozenset({"http", "https"})


# The user-facing half of each denial. Kept as a table beside the code that
# emits it so `scripts/check_i18n.py` can walk it and fail on a missing pair,
# which is what stops a new denial shipping in one language only.
_DENY_MESSAGES: dict[str, dict[str, str]] = {
    "unparseable": {
        "en": "the URL could not be parsed ({detail})",
        "zh": "这个 URL 无法解析（{detail}）",
    },
    "scheme": {
        "en": "the '{scheme}' scheme is not allowed; only {allowed} may be requested",
        "zh": "不允许 '{scheme}' 协议；只能请求 {allowed}",
    },
    "no_host": {"en": "the URL has no host", "zh": "这个 URL 没有主机名"},
    "host_denied": {
        "en": (
            "refusing to request {host}: it is {reason}. "
            "Add it to EGRESS_ALLOWED_HOSTS if this is intentional."
        ),
        "zh": (
            "拒绝请求 {host}：它是{reason}。"
            "如果这是有意为之，请把它加入 EGRESS_ALLOWED_HOSTS。"
        ),
    },
    "redirect": {
        "en": "redirect to a blocked destination: {reason}",
        "zh": "重定向到了被拦截的目标：{reason}",
    },
}

#: The host classes named in :func:`_blocked_reason`, and their Chinese
#: renderings. Separate from the sentences above because one fragment appears in
#: more than one sentence, and translating it once is what keeps them consistent.
#: English host-class fragments, as returned by :func:`_blocked_reason`, mapped
#: to their Chinese form. Keyed by the English text because that is what the
#: classification produces; `check_i18n` walks the values for the translations.
_HOST_REASONS: dict[str, str] = {
    "the unspecified address": "未指定地址",
    "a loopback address": "回环地址",
    "the cloud instance-metadata endpoint (169.254.169.254)": (
        "云实例元数据端点（169.254.169.254）"
    ),
    "a link-local address": "链路本地地址",
    "a multicast address": "组播地址",
    "a private address": "私有地址",
    "a reserved address": "保留地址",
    "an empty host": "空主机名",
}


def _localize_host_reason(reason: str) -> str:
    """Translate the English fragment embedded in a classification.

    Longest-first, so the metadata sentence is not half-replaced by the shorter
    link-local one. Anything not in the table is left alone: a fragment that
    appears in a sentence the table does not cover should show up as English, not
    vanish.
    """
    for key in sorted(_HOST_REASONS, key=len, reverse=True):
        if key in reason:
            return reason.replace(key, _HOST_REASONS[key])
    return reason


@dataclass(frozen=True)
class EgressDecision:
    """Verdict for one URL, in two renderings.

    The split is deliberate rather than redundant:

    * :attr:`reason` is what a **model** reads, because it travels back through
      the tool result. It stays English -- see `docs/current/i18n.md` section 2,
      "text shown to a model is always English".
    * :attr:`reason_localized` is the same verdict for the parts of the product a
      *person* reads.

    Both are filled by :meth:`deny`, so a new denial cannot end up with only one.
    """

    allowed: bool
    reason: str = ""
    reason_localized: str = ""

    def __bool__(self) -> bool:  # allows `if not policy.check(url):`
        return self.allowed

    @classmethod
    def allow(cls) -> EgressDecision:
        return cls(True)

    @classmethod
    def deny(cls, reason: str, localized: str = "") -> EgressDecision:
        """Both renderings at once; an omitted one mirrors the English."""
        return cls(False, reason, localized or reason)


def _blocked_reason(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """Why this address must not be reached, or ``""`` if it is fine.

    Ordered from most-specific to least, because the messages are user-facing
    and "it is the cloud metadata endpoint" is far more useful than
    "it is link-local".
    """
    if address.is_unspecified:
        return "the unspecified address"
    if address.is_loopback:
        return "a loopback address"
    if address.is_link_local:
        # 169.254.0.0/16 on v4 is where every major cloud puts instance
        # credentials. Calling it out by name because this is the single most
        # valuable SSRF target there is.
        if str(address).startswith("169.254.169.254"):
            return "the cloud instance-metadata endpoint (169.254.169.254)"
        return "a link-local address"
    if address.is_multicast:
        return "a multicast address"
    if address.is_private:
        return "a private address"
    if address.is_reserved:
        return "a reserved address"
    return ""


def classify_host(host: str) -> str:
    """Return a denial reason for ``host``, or ``""`` when it is reachable.

    ``host`` may be a literal address or a name; names are resolved and every
    result is checked. IPv4-mapped IPv6 (``::ffff:127.0.0.1``) is unwrapped first,
    because that is the standard way to slip a loopback past a naive check.
    """
    if not host:
        return "an empty host"

    candidate = host.strip().strip("[]")
    # A bare name that happens to end in a dot is still the same name.
    candidate = candidate.rstrip(".")

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        address = None

    if address is not None:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return _blocked_reason(address)

    # A name. Resolve it and check what it actually points at — "localhost" and
    # "metadata.google.internal" are just names until they are not.
    try:
        infos = socket.getaddrinfo(candidate, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Unresolvable names are refused rather than passed through. Letting the
        # HTTP client try would move the decision into a layer that does not
        # report back to us.
        return f"a name that does not resolve ({candidate})"

    if not infos:
        return f"a name that resolves to nothing ({candidate})"

    seen: set[str] = set()
    for info in infos:
        raw = info[4][0]
        if raw in seen:
            continue
        seen.add(raw)
        try:
            resolved = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if isinstance(resolved, ipaddress.IPv6Address) and resolved.ipv4_mapped:
            resolved = resolved.ipv4_mapped
        reason = _blocked_reason(resolved)
        if reason:
            return f"{reason} ({candidate} resolves to {raw})"
    return ""


def _deny(key: str, localized_reason: str = "", **values: object) -> EgressDecision:
    """One denial, rendered for the model and for a person.

    The English sentence is built here rather than at each call site so that the
    two renderings are always produced together.

    ``reason`` is special: the classification returns an English fragment, so the
    Chinese sentence must be given the *translated* fragment instead. Substituting
    the English one would produce a Chinese sentence with an English clause in the
    middle, which looks finished and is not.
    """
    from app.runtime.ai.messages import render

    english = render(_DENY_MESSAGES, key, "en", **values)
    chinese_values = dict(values)
    if localized_reason:
        chinese_values["reason"] = localized_reason
    chinese = render(_DENY_MESSAGES, key, "zh", **chinese_values)
    return EgressDecision.deny(english, chinese)


class EgressPolicy:
    """Decides whether an outbound URL may be requested.

    Deliberately synchronous: the decision is a cheap address check, and doing it
    inline keeps the call site honest about the fact that nothing leaves the
    process until it returns.
    """

    def check(self, url: str) -> EgressDecision:
        raise NotImplementedError


@dataclass
class NetworkEgressPolicy(EgressPolicy):
    """Default policy: public HTTP(S) only, with an explicit exemption list.

    ``allowed_hosts`` exists because blocking private addresses outright breaks
    two legitimate cases — a workflow calling a service on the same machine, and
    a self-hosted model endpoint. An allowlist is the right shape for those: it
    grants exactly the hosts the operator named and nothing else, whereas
    ``allow_private=True`` re-opens the whole LAN to satisfy one of them.

    Entries are either a hostname (any port) or ``host:port``. Use the port form
    for a local gateway: the operator wants the workflow to reach the line API,
    not the admin API listening on another port of the same machine.
    """

    allow_private: bool = False
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)

    def check(self, url: str) -> EgressDecision:
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            return _deny("unparseable", detail=exc)

        scheme = (parts.scheme or "").lower()
        if scheme not in ALLOWED_SCHEMES:
            return _deny(
                "scheme", scheme=scheme or "(none)", allowed=", ".join(sorted(ALLOWED_SCHEMES))
            )

        host = parts.hostname or ""
        if not host:
            return _deny("no_host")

        bare_host = host.rstrip(".").lower()
        if bare_host in self.allowed_hosts:
            return EgressDecision.allow()

        # A `host:port` entry opens exactly one service. Granting a bare
        # loopback name would also grant every admin API on the machine, which
        # is a very different thing from "let the workflow call this gateway".
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError:
            return _deny("unparseable", detail="invalid port")
        if f"{bare_host}:{port}" in self.allowed_hosts:
            return EgressDecision.allow()

        if self.allow_private:
            return EgressDecision.allow()

        reason = classify_host(host)
        if reason:
            return _deny(
                "host_denied",
                host=host,
                reason=reason,
                localized_reason=_localize_host_reason(reason),
            )
        return EgressDecision.allow()


def build_egress_policy(settings: Settings | None = None) -> NetworkEgressPolicy:
    """Construct the policy from configuration."""
    settings = settings or get_settings()
    return NetworkEgressPolicy(
        allow_private=bool(settings.egress_allow_private),
        allowed_hosts=frozenset(
            host.strip().lower() for host in settings.egress_allowed_hosts if host.strip()
        ),
    )


#: Process-wide policy. Swapped in tests, and the seam where a future
#: proxy-backed or per-tenant policy plugs in.
_policy: EgressPolicy | None = None


def get_egress_policy() -> EgressPolicy:
    global _policy
    if _policy is None:
        _policy = build_egress_policy()
    return _policy


def set_egress_policy(policy: EgressPolicy | None) -> None:
    """Override the process policy. Passing ``None`` restores the configured one."""
    global _policy
    _policy = policy


__all__ = [
    "ALLOWED_SCHEMES",
    "EgressDecision",
    "EgressPolicy",
    "NetworkEgressPolicy",
    "build_egress_policy",
    "classify_host",
    "get_egress_policy",
    "set_egress_policy",
]
