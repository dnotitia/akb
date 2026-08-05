"""Shared canonical validator for external-git mirror remotes.

This module is the ONE place that decides whether a user-supplied external-git
remote is safe to touch. It closes the transport, credential-hygiene, and
host-resolution surface for external-git mirror remotes.

Design contract highlights:

* **Pure vs DNS split.** ``parse_and_canonicalize`` and ``validate_branch`` are
  pure (no I/O) and cheap — a caller runs them inline. ``resolve_and_check_host``
  does DNS + IP classification and is *bounded* (a disposable resolver with a
  hard lifetime), so a caller can run it off the event loop without risking an
  un-cancellable hang. ``validate`` composes all three; its ``resolve`` flag
  lets a caller take just the pure part synchronously.
* **Fail-closed host policy.** Every resolved address must be globally-routable
  unicast. The only exception is an explicit ``(host, CIDR set, ports)`` rule in
  settings, and even then every resolved IP must fall inside the rule's CIDRs —
  a host-only match never exempts the global-unicast check.
* **No secrets in errors.** ``ExternalGitPolicyError`` messages never contain the
  raw URL, its userinfo, or the auth token; they name the violation class only
  (plus non-secret specifics such as a scheme name, port, or resolved IP class).
* **Transient vs permanent.** DNS timeout / SERVFAIL / NXDOMAIN raise
  ``ExternalGitTransientError`` (the caller backs off) — never a policy
  violation, so they never quarantine a vault.

Stage-1 scope: this module + config + unit tests only. Wiring into
``document_service`` (create path), the poller, and the hermetic git runner is
deferred to later stages.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import re
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from app.exceptions import AKBError, ValidationError

if TYPE_CHECKING:
    from app.config import ExternalGitHostRule, Settings

# ── Limits / charsets ────────────────────────────────────────────────
# Auth tokens are opaque bearer secrets; they must be single-line and
# length-bounded before they can ever reach a git config extraHeader (stage 2).
# CR/LF/NUL could otherwise break out of the header/argv boundary.
_MAX_AUTH_TOKEN_LEN = 4096
_FORBIDDEN_TOKEN_CHARS = ("\r", "\n", "\x00")

# Branch/ref limits.
_MAX_BRANCH_LEN = 255
_MAX_BRANCH_COMPONENT_LEN = 255
# Conservative ref charset — deliberately stricter than git's own rules. Normal
# branch names (main, release/1.2.3, feature/foo-bar, v1.0.0) all pass; the
# excluded characters ('@', '{', '}', '~', '^', ':', '?', '*', '[', '\\',
# space, control) are exactly what would let a branch value be parsed as a
# git option or ref-shorthand instead of a plain name.
_BRANCH_CHARSET = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/"
)

# Characters never allowed anywhere in a raw URL. Checked BEFORE urlsplit,
# which otherwise silently strips tab/newline and leading/trailing control+space
# (WHATWG) — letting disallowed characters slip past every later check.
_DISALLOWED_URL_CHARS = frozenset("\\ \t\r\n\x7f") | frozenset(chr(c) for c in range(0x20))

# Final ASCII DNS-name shape (post-IDNA). Total <= 253, each label 1..63 of
# [a-z0-9-] not starting/ending with a hyphen.
_ASCII_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))*$"
)

_DEFAULT_PORTS = {"https": 443, "http": 80}

# Explicit non-routable ranges, enumerated below. These give a precise
# rejection reason and are version-robust; a final `not is_global` backstop
# below catches anything these miss.
_DENY_V4: dict[str, list[ipaddress.IPv4Network]] = {
    "unspecified": [ipaddress.IPv4Network("0.0.0.0/8")],
    "loopback": [ipaddress.IPv4Network("127.0.0.0/8")],
    "link-local": [ipaddress.IPv4Network("169.254.0.0/16")],
    "cgnat": [ipaddress.IPv4Network("100.64.0.0/10")],
    "private": [
        ipaddress.IPv4Network("10.0.0.0/8"),
        ipaddress.IPv4Network("172.16.0.0/12"),
        ipaddress.IPv4Network("192.168.0.0/16"),
    ],
    "benchmarking": [ipaddress.IPv4Network("198.18.0.0/15")],
}
_DENY_V6: dict[str, list[ipaddress.IPv6Network]] = {
    "unspecified": [ipaddress.IPv6Network("::/128")],
    "loopback": [ipaddress.IPv6Network("::1/128")],
    "link-local": [ipaddress.IPv6Network("fe80::/10")],
    "unique-local": [ipaddress.IPv6Network("fc00::/7")],
    "benchmarking": [ipaddress.IPv6Network("2001:2::/48")],
}


# ── Result types ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class CanonicalRemote:
    """Result of the pure parse+canonicalize step (no DNS performed)."""

    canonical_url: str
    scheme: str
    host: str  # ASCII/IDNA, lowercased; IPv6 literals WITHOUT brackets
    port: int


@dataclass(frozen=True)
class ValidatedRemote:
    """A remote that passed the full policy (parse + branch + host resolution)."""

    canonical_url: str
    scheme: str
    host: str
    port: int
    pinned_ips: tuple[str, ...]  # validated resolved addresses (empty if resolve=False)
    branch: str


# ── Errors ───────────────────────────────────────────────────────────
class ExternalGitPolicyError(ValidationError):
    """A permanent external-git policy violation.

    IS-A ``ValidationError`` (→ ``ValueError`` + ``AKBError``) so it maps to MCP
    ``invalid_argument`` / REST 422 exactly like every other client-input
    reject, and the many ``except ValueError`` sites keep catching it.

    Invariant: the message NEVER contains the raw URL, its userinfo, or the auth
    token — only the violation class (and non-secret specifics such as a scheme
    name, port number, or resolved IP class).
    """


class ExternalGitTransientError(AKBError):
    """A transient host-resolution failure (DNS timeout / SERVFAIL / NXDOMAIN /
    resolver unavailable).

    Explicitly NOT a policy violation: the caller backs off and retries rather
    than quarantining the vault. Distinct base (not
    ``ValidationError``) so the two are never confused. Maps to HTTP 503.
    """

    def __init__(self, message: str):
        super().__init__(message, status_code=503, code="external_git_transient")


# ── Resolver (bounded, disposable) ───────────────────────────────────
class HostResolver(Protocol):
    """A disposable, hard-lifetime host resolver.

    ``resolve`` MUST return within ``timeout`` seconds no matter what the
    upstream resolver does, raising ``ExternalGitTransientError`` on
    timeout/failure. The reference implementation abandons a throwaway worker on
    timeout so the caller is never pinned past the deadline.
    """

    def resolve(self, host: str, *, timeout: float) -> list[str]: ...


def _getaddrinfo_all(host: str) -> list[str]:
    infos = socket.getaddrinfo(
        host, None, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    # De-dup while preserving order (getaddrinfo repeats per socktype).
    seen: set[str] = set()
    out: list[str] = []
    for ai in infos:
        ip = str(ai[4][0])
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


class _BoundedThreadPoolResolver:
    """Default resolver: runs ``getaddrinfo`` on a **truly bounded** thread pool.

    ``getaddrinfo`` has no per-call timeout and cannot be cancelled in-thread, so
    a wedged upstream resolver keeps a worker blocked until the OS resolver's own
    timeout fires. Under such a wedge two things must stay bounded:

    * **Concurrency** — at most ``max_workers`` resolutions run at once (the pool
      size).
    * **Backlog** — the pending work queue must NOT grow without bound. A naive
      "submit every call, cancel the future on timeout" scheme fails this:
      ``ThreadPoolExecutor`` does not evict a cancelled-but-already-queued work
      item from its internal queue, so with every worker wedged each new (doomed)
      call still enqueues a work item and memory grows without bound
      (reproduced with ``max_workers=1``: 4 timeouts → 3 stuck queued items).
      ``external_git_max_concurrent_resolutions`` was then not actually enforced.

    The fix is a capacity semaphore sized to the pool, acquired BEFORE submit:

    * A permit is taken (waiting at most until the call's deadline) *before* any
      ``submit``. If no permit frees in time — every worker wedged — the call
      raises ``ExternalGitTransientError`` and **never submits**, so the queue
      cannot grow. Capacity pressure is transient (back off), never a policy
      fault, so it never quarantines a vault.
    * On success the call submits and waits ``future.result`` for the remainder
      of its deadline; it is never pinned past ``timeout`` (HostResolver
      contract).
    * The permit is released from the future's **done-callback**, i.e. only when
      the *actual* ``getaddrinfo`` finishes (success/failure) — even if this
      caller already timed out and walked away. Holding the slot until the real
      work drains is exactly what keeps the invariant **running + queued <=
      max_workers** true at every instant: submitted-but-unfinished work equals
      the permits held, which is capped.

    Truly *killing* a wedged ``getaddrinfo`` (vs. bounding how many can be wedged
    and refusing the rest) would require a resolver subprocess; the design
    accepts a genuine bounded pool here and defers a process-based resolver.
    Callers inject via the ``resolver`` parameter, so that upgrade stays drop-in.
    Threads are named for observability; the pool is process-lifetime.
    """

    def __init__(self, max_workers: int):
        self._max_workers = max(1, int(max_workers))
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="extgit-resolve"
        )
        # Capacity gate == pool size. A permit is held from just-before-submit
        # until the submitted getaddrinfo actually completes (released in the
        # done-callback), so submitted-but-unfinished work (running + queued)
        # can never exceed the cap and the pending queue stays bounded.
        self._capacity = threading.BoundedSemaphore(self._max_workers)

    def resolve(self, host: str, *, timeout: float) -> list[str]:
        deadline = time.monotonic() + timeout
        # 1. Reserve a slot BEFORE submitting. Under a full wedge no permit frees
        #    and we fail transient WITHOUT enqueuing work (the fix for the
        #    unbounded-backlog growth). Budget the wait so the whole call still
        #    honours ``timeout`` (HostResolver contract: return within timeout).
        if not self._capacity.acquire(timeout=max(0.0, timeout)):
            raise ExternalGitTransientError(
                "host resolution capacity exceeded; back off and retry"
            )
        try:
            future = self._executor.submit(_getaddrinfo_all, host)
        except BaseException:
            # No work item was created, so no done-callback will fire — release
            # the permit here so the slot is not leaked, then surface transient.
            self._capacity.release()
            raise ExternalGitTransientError("host resolution unavailable") from None
        # From here the permit is owned by the done-callback: it fires exactly
        # once when the getaddrinfo finishes, releasing the slot even if THIS
        # caller times out below and leaves the work running.
        future.add_done_callback(self._release_capacity)

        remaining = deadline - time.monotonic()
        try:
            result = future.result(timeout=max(0.0, remaining))
        except concurrent.futures.TimeoutError:
            # Do NOT cancel: let the running getaddrinfo drain and release its
            # slot via the callback. Cancelling a still-queued item would free
            # the slot early and could leave a stale item sitting in the pool
            # queue — the very accumulation this design prevents.
            raise ExternalGitTransientError(
                f"host resolution timed out after {timeout:g}s"
            ) from None
        except BaseException:  # gaierror / SERVFAIL / NXDOMAIN → transient
            raise ExternalGitTransientError("host resolution failed") from None
        if not result:
            raise ExternalGitTransientError("host did not resolve to any address")
        return result

    def _release_capacity(
        self, _future: "concurrent.futures.Future[list[str]]"
    ) -> None:
        """Return the capacity permit reserved in ``resolve`` — invoked exactly
        once per submitted future, when the real getaddrinfo has finished."""
        self._capacity.release()


# One bounded pool per distinct concurrency cap (a single Settings in prod; tests
# may build several). Lazily created so import never spins up threads.
_default_resolvers: dict[int, _BoundedThreadPoolResolver] = {}
_default_resolver_lock = threading.Lock()


def _get_default_resolver(max_concurrent: int) -> HostResolver:
    cap = max(1, int(max_concurrent))
    with _default_resolver_lock:
        resolver = _default_resolvers.get(cap)
        if resolver is None:
            resolver = _BoundedThreadPoolResolver(cap)
            _default_resolvers[cap] = resolver
        return resolver


# ── Pure: parse + canonicalize ───────────────────────────────────────
def parse_and_canonicalize(url: str, *, settings: "Settings") -> CanonicalRemote:
    """Strictly parse and canonicalize a remote URL. PURE — performs no DNS.

    Enforces: scheme allowlist (https always; http only when
    ``settings.external_git_allow_http``), no userinfo, no control/whitespace/
    backslash, no query, no fragment, no zone-id, a permitted port, and returns
    a reconstructed ASCII/IDNA canonical form. Non-standard numeric host forms
    (decimal/hex/octal) are intentionally left for the resolver to expand and
    classify (resolution is authoritative).
    """
    if not isinstance(url, str):
        raise ExternalGitPolicyError("external_git URL must be a string")
    if not url.strip():
        raise ExternalGitPolicyError("external_git URL must not be empty")

    # 1. Raw-string hygiene BEFORE urlsplit (see _DISALLOWED_URL_CHARS).
    if any(ch in _DISALLOWED_URL_CHARS for ch in url):
        raise ExternalGitPolicyError(
            "external_git URL contains a control, whitespace, or backslash character"
        )

    parts = urlsplit(url)

    # 2. Scheme allowlist — single source of truth is external_git_allow_http.
    scheme = parts.scheme.lower()
    if scheme == "https":
        pass
    elif scheme == "http":
        if not settings.external_git_allow_http:
            raise ExternalGitPolicyError(
                "external_git URL scheme 'http' is disabled "
                "(set external_git_allow_http to enable)"
            )
    else:
        allowed = "https or http" if settings.external_git_allow_http else "https"
        raise ExternalGitPolicyError(
            f"external_git URL scheme {scheme or '(none)'!r} is not permitted; "
            f"only {allowed} is allowed"
        )

    # 3. No userinfo — credentials belong in auth_token, never in the URL.
    if "@" in parts.netloc:
        raise ExternalGitPolicyError(
            "external_git URL must not embed userinfo/credentials (use auth_token)"
        )

    # 4. No query / no fragment (default reject; abnormal for a git remote).
    if parts.query:
        raise ExternalGitPolicyError("external_git URL must not contain a query string")
    if parts.fragment:
        raise ExternalGitPolicyError("external_git URL must not contain a fragment")

    # 4b. No '=' in the path. The canonical URL is injected into the
    #     hermetic runner as ``-c http.<url>.<key>=<value>`` and
    #     ``--config-env=http.<url>.extraHeader=<ENV>``; git splits a ``-c``
    #     token on the FIRST '=', so a '=' inside <url> would corrupt the config
    #     key. Fail closed with a clear reason rather than silently mis-injecting
    #     (a delimiter-safe form is not expressible via git's -c grammar).
    if "=" in parts.path:
        raise ExternalGitPolicyError(
            "external_git URL path must not contain '=' "
            "(unsupported by the hermetic git config injection)"
        )

    # 5. Host.
    try:
        raw_host = parts.hostname
    except ValueError:
        raw_host = None
    if not raw_host:
        raise ExternalGitPolicyError("external_git URL has no host")
    if "%" in raw_host:
        # zone-id (fe80::1%eth0) or percent-encoding in host — both abnormal.
        raise ExternalGitPolicyError("external_git URL host contains a disallowed character")

    canonical_host = _canonicalize_host(raw_host)

    # 6. Port (urlsplit validates numeric range; a bad port raises ValueError).
    try:
        explicit_port = parts.port
    except ValueError:
        raise ExternalGitPolicyError("external_git URL has an invalid port")
    if explicit_port is not None and not (1 <= explicit_port <= 65535):
        raise ExternalGitPolicyError("external_git URL has an invalid port")
    default_port = _DEFAULT_PORTS[scheme]
    port = explicit_port if explicit_port is not None else default_port

    # 7. Port policy. An allowlisted internal host must enumerate EVERY
    #    port it may be reached on — the scheme's standard port is NOT
    #    auto-allowed for a rule host, so a rule with empty ``ports`` allows no
    #    connection at all. A host WITHOUT a rule may
    #    only be reached on the scheme's standard port.
    rule = _find_host_rule(canonical_host, settings)
    if rule is not None:
        if port not in rule.ports:
            raise ExternalGitPolicyError(
                f"external_git URL port {port} is not permitted for this host"
            )
    elif port != default_port:
        raise ExternalGitPolicyError(
            f"external_git URL port {port} is not permitted for this host"
        )

    # 8. Reconstruct a canonical ASCII URL: default port omitted, IPv6 literal
    #    re-bracketed, path preserved verbatim (no query/fragment survive).
    netloc = _format_netloc(canonical_host, port, default_port)
    canonical_url = f"{scheme}://{netloc}{parts.path}"

    return CanonicalRemote(
        canonical_url=canonical_url, scheme=scheme, host=canonical_host, port=port
    )


def _canonicalize_host(raw_host: str) -> str:
    """Return the canonical ASCII/IDNA, lowercased host. IPv6/IPv4 literals are
    normalized via ``ipaddress``; domain names are IDNA-encoded."""
    host = raw_host.lower()
    if ":" in host:
        try:
            return str(ipaddress.IPv6Address(host))
        except ValueError:
            raise ExternalGitPolicyError("external_git URL has a malformed IPv6 host")
    # Canonical dotted-quad IPv4 literal (strict: no leading zeros, no shorthand).
    # Non-canonical numeric forms fall through to the domain path and are caught
    # authoritatively at DNS resolution time.
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        pass
    return _idna_encode(host)


def _idna_encode(host: str) -> str:
    if host.endswith("."):
        raise ExternalGitPolicyError("external_git URL host must not end with a dot")
    if host.startswith(".") or ".." in host:
        raise ExternalGitPolicyError("external_git URL host has an empty label")
    if host.isascii():
        ascii_host = host
    else:
        try:
            import idna

            ascii_host = idna.encode(host, uts46=True).decode("ascii").lower()
        except Exception:
            raise ExternalGitPolicyError("external_git URL host is not a valid domain name")
    if not _ASCII_HOST_RE.fullmatch(ascii_host):
        raise ExternalGitPolicyError("external_git URL host is not a valid domain name")
    return ascii_host


def _format_netloc(host: str, port: int, default_port: int) -> str:
    hostpart = f"[{host}]" if ":" in host else host  # IPv6 literal needs brackets
    return hostpart if port == default_port else f"{hostpart}:{port}"


def _find_host_rule(canonical_host: str, settings: "Settings") -> "ExternalGitHostRule | None":
    for rule in settings.external_git_host_allowlist:
        if rule.host == canonical_host:
            return rule
    return None


# ── Pure: branch ─────────────────────────────────────────────────────
def validate_branch(branch: str) -> str:
    """Validate a branch name for literal use as ``refs/heads/<branch>`` and as
    a standalone git argv. Returns the branch unchanged on success.

    Rejects a leading ``-`` (would be parsed as an option), ref shorthand
    (``@{`` incl. ``@{-n}``), a ``..`` path segment, and other malformed refs.
    Deliberately stricter than ``git check-ref-format`` (which EXPANDS
    shorthand); we never shell out to validate.
    """
    if not isinstance(branch, str):
        raise ExternalGitPolicyError("external_git branch must be a string")
    if not branch:
        raise ExternalGitPolicyError("external_git branch must not be empty")
    if len(branch) > _MAX_BRANCH_LEN:
        raise ExternalGitPolicyError(f"external_git branch exceeds {_MAX_BRANCH_LEN} characters")
    # A leading '-' would make git parse the branch as a command-line option
    # rather than a ref name (a scheme-independent argv-confusion risk).
    if branch.startswith("-"):
        raise ExternalGitPolicyError("external_git branch must not start with '-'")
    # Ref shorthand that `check-ref-format --branch` would EXPAND: @{-N}
    # (previous checkout), @{...} (reflog/upstream). Checked early for a clear
    # message (the charset below also excludes '@' and '{').
    if "@{" in branch:
        raise ExternalGitPolicyError("external_git branch must not contain '@{'")
    if any(ch not in _BRANCH_CHARSET for ch in branch):
        raise ExternalGitPolicyError("external_git branch contains a disallowed character")
    # Structural git-ref rules on the literal refs/heads/<branch> form.
    if branch.startswith("/") or branch.endswith("/"):
        raise ExternalGitPolicyError("external_git branch must not start or end with '/'")
    if "//" in branch:
        raise ExternalGitPolicyError("external_git branch must not contain '//'")
    if ".." in branch:
        raise ExternalGitPolicyError("external_git branch must not contain '..'")
    if branch.endswith("."):
        raise ExternalGitPolicyError("external_git branch must not end with '.'")
    for component in branch.split("/"):
        if not component:
            raise ExternalGitPolicyError("external_git branch has an empty path component")
        if len(component) > _MAX_BRANCH_COMPONENT_LEN:
            raise ExternalGitPolicyError("external_git branch component is too long")
        if component in (".", ".."):
            raise ExternalGitPolicyError("external_git branch component must not be '.' or '..'")
        if component.startswith("."):
            raise ExternalGitPolicyError("external_git branch component must not start with '.'")
        if component.endswith(".lock"):
            raise ExternalGitPolicyError("external_git branch component must not end with '.lock'")
    return branch


# ── DNS + host safety ────────────────────────────────────────────────
def resolve_and_check_host(
    host: str, *, settings: "Settings", resolver: HostResolver | None = None
) -> list[str]:
    """Resolve ``host`` and verify EVERY address is safe to connect to.

    Returns the list of validated (normalized) IP strings for later DNS-pinning.
    Raises ``ExternalGitPolicyError`` for a policy violation (non-routable
    target, cluster-internal name, out-of-CIDR allowlisted host, an address in an
    operator pod/service deny-CIDR) and ``ExternalGitTransientError`` for a DNS
    timeout/failure.
    """
    resolver = resolver or _get_default_resolver(
        settings.external_git_max_concurrent_resolutions
    )

    literal = _as_ip_literal(host)

    # Cluster-internal names are denied up front, regardless of resolved IP.
    if literal is None:
        _reject_cluster_internal(host, settings)

    rule = _find_host_rule(host, settings)
    deny_networks = _deny_networks(settings)

    if literal is not None:
        addresses = [literal]
    else:
        addresses = resolver.resolve(host, timeout=settings.external_git_resolver_timeout)

    validated: list[str] = []
    for ip in addresses:
        _check_address_allowed(ip, rule=rule, deny_networks=deny_networks)
        validated.append(_normalize_ip(ip))
    return validated


def _deny_networks(
    settings: "Settings",
) -> tuple["ipaddress.IPv4Network | ipaddress.IPv6Network", ...]:
    """Operator pod/service deny-CIDRs — applied BEFORE any allowlist so a
    pod/service network can never be allow-listed by accident."""
    return tuple(
        ipaddress.ip_network(c, strict=False)
        for c in getattr(settings, "external_git_deny_cidrs", ()) or ()
    )


def _as_ip_literal(host: str) -> str | None:
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return None


def _reject_cluster_internal(host: str, settings: "Settings") -> None:
    lowered = host.lower()
    if lowered == "localhost":
        raise ExternalGitPolicyError("external_git host 'localhost' is not permitted")
    for suffix in settings.external_git_cluster_dns_suffixes:
        if suffix and (lowered == suffix.lstrip(".") or lowered.endswith(suffix)):
            raise ExternalGitPolicyError(
                "external_git host is a cluster-internal name and is not permitted"
            )


def _check_address_allowed(
    ip: str,
    *,
    rule: "ExternalGitHostRule | None",
    deny_networks: tuple["ipaddress.IPv4Network | ipaddress.IPv6Network", ...] = (),
) -> None:
    """Raise ``ExternalGitPolicyError`` unless ``ip`` is permitted.

    Order of checks:

    1. IPv4-in-IPv6 encapsulation (IPv4-mapped ``::ffff:0:0/96``, 6to4
       ``2002::/16``, Teredo ``2001:0::/32``) is rejected OUTRIGHT — before any
       allow decision, even inside an allowlist rule, and even when the embedded
       v4 looks globally routable. Legit git hosts never need these forms, so
       rejecting them outright is defense-in-depth.
    2. Operator pod/service **deny**-CIDRs are refused BEFORE the allowlist so a
       pod/service network can never be admitted by an allowlist rule by
       accident.
    3. With an allowlist ``rule`` the address must fall inside one of the rule's
       CIDRs — a host-only match never exempts the check (the CIDR set IS the
       constraint).
    4. Otherwise the address must be globally-routable unicast.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise ExternalGitPolicyError("external_git host resolved to an unparseable address")

    enc = _encapsulation_reason(addr)
    if enc is not None:
        raise ExternalGitPolicyError(
            f"external_git host resolves to a non-routable address ({enc})"
        )

    # Deny-CIDRs win over any allow (applied before the allowlist rule).
    if any(addr in n for n in deny_networks):
        raise ExternalGitPolicyError(
            "external_git host resolves into an operator-denied CIDR "
            "(pod/service network)"
        )

    if rule is not None:
        nets = [ipaddress.ip_network(c, strict=False) for c in rule.cidrs]
        if any(addr in n for n in nets):
            return
        raise ExternalGitPolicyError(
            "external_git host resolved to an address outside its allowlisted CIDRs"
        )

    reason = _routable_reject_reason(addr)
    if reason is not None:
        raise ExternalGitPolicyError(
            f"external_git host resolves to a non-routable address ({reason})"
        )


def _encapsulation_reason(
    addr: "ipaddress.IPv4Address | ipaddress.IPv6Address",
) -> str | None:
    """Reason string if ``addr`` is an IPv4-in-IPv6 encapsulation form; else None.

    Prefers the embedded v4's specific non-global reason for a precise message
    (so ``::ffff:127.0.0.1`` → ``loopback``, ``::ffff:10.0.0.1`` → ``private``),
    falling back to the generic ``ipv4-encapsulation`` when the embedded v4
    itself looks global (e.g. ``::ffff:8.8.8.8``) — the encapsulation is refused
    regardless of what it wraps."""
    if not isinstance(addr, ipaddress.IPv6Address):
        return None
    embedded = addr.ipv4_mapped or addr.sixtofour
    if embedded is None and addr.teredo is not None:
        embedded = addr.teredo[1]  # Teredo client IPv4
    if embedded is None:
        return None
    return _v4_nonglobal_reason(embedded) or "ipv4-encapsulation"


def _v4_nonglobal_reason(addr: "ipaddress.IPv4Address") -> str | None:
    """Reason string if a bare IPv4 address is NOT globally-routable unicast."""
    if addr.is_multicast:
        return "multicast"
    for reason, nets4 in _DENY_V4.items():
        if any(addr in n for n in nets4):
            return reason
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link-local"
    if addr.is_unspecified:
        return "unspecified"
    if addr.is_reserved:
        return "reserved"
    if addr.is_private:
        return "private"
    if not addr.is_global:
        return "not-globally-routable"
    return None


def _routable_reject_reason(
    addr: "ipaddress.IPv4Address | ipaddress.IPv6Address",
) -> str | None:
    """Return a reason string if ``addr`` is NOT globally-routable unicast; else
    None. Encapsulation forms are handled by ``_encapsulation_reason`` before
    this is reached."""
    if isinstance(addr, ipaddress.IPv4Address):
        return _v4_nonglobal_reason(addr)
    # Multicast first: ff00::/8 — the is_global backstop can misjudge it.
    if addr.is_multicast:
        return "multicast"
    for reason, nets6 in _DENY_V6.items():
        if any(addr in n for n in nets6):
            return reason
    # Flag-based backstops (catch anything the explicit list missed).
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link-local"
    if addr.is_unspecified:
        return "unspecified"
    if addr.is_reserved:
        return "reserved"
    if addr.is_private:
        return "private"
    if not addr.is_global:
        return "not-globally-routable"
    return None


def _normalize_ip(ip: str) -> str:
    """Canonical compressed string form of a resolved IP (for pinning)."""
    return str(ipaddress.ip_address(ip))


# ── Composed entry point ─────────────────────────────────────────────
def validate(
    data: Mapping[str, object],
    *,
    settings: "Settings",
    resolve: bool = True,
    resolver: HostResolver | None = None,
) -> ValidatedRemote:
    """Full validation of an external_git config block (create-time dict) OR a
    ``vault_external_git`` row.

    Shape-validates the mapping, then runs the pure parse + branch checks and
    (when ``resolve`` is True) the DNS + host-safety check. The pure/DNS split
    lets a caller run the cheap part on the event loop and the DNS part off it.

    Accepts either key spelling: ``url``/``branch`` (create dict) or
    ``remote_url``/``remote_branch`` (DB row).
    """
    if not isinstance(data, Mapping):
        raise ExternalGitPolicyError("external_git must be an object")

    url = _pick(data, "url", "remote_url")
    if not isinstance(url, str):
        raise ExternalGitPolicyError("external_git.url must be a string")

    branch_val = _pick(data, "branch", "remote_branch")
    if branch_val is None:
        branch_val = "main"
    if not isinstance(branch_val, str):
        raise ExternalGitPolicyError("external_git.branch must be a string")

    _validate_auth_token(data.get("auth_token"))
    _validate_poll_interval(data.get("poll_interval_secs"), settings=settings)

    canonical = parse_and_canonicalize(url, settings=settings)
    branch = validate_branch(branch_val)

    if resolve:
        pinned = tuple(
            resolve_and_check_host(canonical.host, settings=settings, resolver=resolver)
        )
    else:
        pinned = ()

    return ValidatedRemote(
        canonical_url=canonical.canonical_url,
        scheme=canonical.scheme,
        host=canonical.host,
        port=canonical.port,
        pinned_ips=pinned,
        branch=branch,
    )


def _pick(data: Mapping[str, object], primary: str, secondary: str) -> object:
    if data.get(primary) is not None:
        return data[primary]
    if data.get(secondary) is not None:
        return data[secondary]
    return None


def _validate_auth_token(token: object) -> None:
    if token is None:
        return
    if not isinstance(token, str):
        raise ExternalGitPolicyError("external_git.auth_token must be a string or null")
    if len(token) > _MAX_AUTH_TOKEN_LEN:
        raise ExternalGitPolicyError(
            f"external_git.auth_token exceeds {_MAX_AUTH_TOKEN_LEN} characters"
        )
    if any(bad in token for bad in _FORBIDDEN_TOKEN_CHARS):
        raise ExternalGitPolicyError("external_git.auth_token must not contain CR, LF, or NUL")


def _validate_poll_interval(value: object, *, settings: "Settings") -> None:
    if value is None:
        return  # create-time default (300) is applied by the caller; a row always has one
    # bool is a subclass of int — reject it explicitly (True == 1).
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExternalGitPolicyError("external_git.poll_interval_secs must be an integer")
    # 60 is a code-owned hard floor; an operator may only RAISE it via
    # external_git_poll_interval_min, never lower it.
    effective_min = max(60, settings.external_git_poll_interval_min)
    if value < effective_min:
        raise ExternalGitPolicyError(
            f"external_git.poll_interval_secs must be >= {effective_min}"
        )
    if value > settings.external_git_poll_interval_max:
        raise ExternalGitPolicyError(
            f"external_git.poll_interval_secs must be <= {settings.external_git_poll_interval_max}"
        )
