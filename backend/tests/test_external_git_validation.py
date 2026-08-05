"""Unit tests for the shared external-git remote validator.

Covers: scheme allowlist, URL hygiene (userinfo/query/fragment/
control/port), canonical reconstruction, IP classification (incl. IPv4-mapped
and decimal/hex forms where the resolver is authoritative), the
``(host, CIDR, ports)`` internal exception, cluster-suffix deny, branch literal
+ shorthand rejection, auth-token shape, poll-interval bounds, the transient-vs-
permanent error split, and the no-secret-in-message invariant.

Pure functions are exercised directly; the DNS leg is exercised with an injected
fake resolver (deterministic) plus a couple of monkeypatched real-resolver cases
for the disposable-resolver timeout/failure contract.
"""
from __future__ import annotations

import socket
import threading

import pytest

from app.config import Settings
from app.exceptions import ValidationError
from app.services import external_git_validation as v

# A definitely-global public address used as the benign default resolution.
_PUBLIC_V4 = "140.82.121.3"
_PUBLIC_V6 = "2606:4700:4700::1111"


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


class _FakeResolver:
    """Deterministic resolver. ``mapping`` gives host → [ip, ...]; unlisted hosts
    resolve to a single global public address so happy paths don't need entries."""

    def __init__(self, mapping: dict[str, list[str]] | None = None) -> None:
        self.mapping = mapping or {}

    def resolve(self, host: str, *, timeout: float) -> list[str]:
        return list(self.mapping.get(host, [_PUBLIC_V4]))


# ── Error taxonomy ───────────────────────────────────────────────────
def test_policy_error_is_validation_error_and_maps_to_422():
    assert issubclass(v.ExternalGitPolicyError, ValidationError)
    assert issubclass(v.ExternalGitPolicyError, ValueError)
    assert v.ExternalGitPolicyError("x").status_code == 422


def test_transient_error_is_distinct_from_policy_error():
    # Must NOT be a policy/validation error — callers back off, never quarantine.
    assert not issubclass(v.ExternalGitTransientError, ValidationError)
    assert not issubclass(v.ExternalGitTransientError, v.ExternalGitPolicyError)
    assert v.ExternalGitTransientError("x").status_code == 503


# ── Scheme allowlist ─────────────────────────────────────────────────
def test_https_is_accepted():
    c = v.parse_and_canonicalize("https://github.com/org/repo.git", settings=_settings())
    assert c.scheme == "https"


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c whoami",
        "ext::sh",
        "file:///etc/passwd",
        "git://example.com/repo.git",
        "ssh://git@example.com/repo.git",
        "ftp://example.com/repo.git",
        "javascript:alert(1)",
        "HTTPS+unix://x",
    ],
)
def test_non_https_schemes_are_rejected(url):
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize(url, settings=_settings())


def test_http_rejected_by_default_but_allowed_when_opted_in():
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize("http://github.com/r.git", settings=_settings())
    c = v.parse_and_canonicalize(
        "http://github.com/r.git", settings=_settings(external_git_allow_http=True)
    )
    assert c.scheme == "http" and c.port == 80


# ── Canonical reconstruction ─────────────────────────────────────────
def test_host_and_scheme_are_lowercased_path_preserved():
    c = v.parse_and_canonicalize("https://GitHub.COM/Org/Repo.git", settings=_settings())
    assert c.canonical_url == "https://github.com/Org/Repo.git"
    assert c.host == "github.com"
    assert c.port == 443


def test_default_port_is_omitted_from_canonical_url():
    c = v.parse_and_canonicalize("https://github.com:443/r.git", settings=_settings())
    assert c.canonical_url == "https://github.com/r.git"
    assert c.port == 443


def test_ipv6_literal_is_normalized_and_rebracketed():
    c = v.parse_and_canonicalize("https://[2001:DB8::1]/r.git", settings=_settings())
    assert c.host == "2001:db8::1"
    assert c.canonical_url == "https://[2001:db8::1]/r.git"


def test_idna_host_is_punycode_encoded():
    c = v.parse_and_canonicalize("https://bücher.example/r.git", settings=_settings())
    assert c.host == "xn--bcher-kva.example"
    assert c.canonical_url == "https://xn--bcher-kva.example/r.git"


# ── URL hygiene ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@github.com/r.git",  # pragma: allowlist secret
        "https://x-access-token:secret@github.com/r.git",  # pragma: allowlist secret
        "https://@github.com/r.git",
    ],
)
def test_userinfo_is_rejected(url):
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize(url, settings=_settings())


def test_query_and_fragment_are_rejected():
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize("https://github.com/r.git?x=1", settings=_settings())
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize("https://github.com/r.git#frag", settings=_settings())


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/a b",      # space
        "https://git\thub.com/r",      # tab (urlsplit would silently strip it)
        "https://github.com/r\n",      # newline
        "https://github.com/r\r",      # carriage return
        "https://github.com/\x00",     # NUL
        "https://github.com\\@evil/r",  # backslash
        "https://github.com/\x7f",     # DEL
    ],
)
def test_control_whitespace_backslash_are_rejected(url):
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize(url, settings=_settings())


def test_ipv6_zone_id_is_rejected():
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize("https://[fe80::1%25eth0]/r.git", settings=_settings())


def test_missing_host_is_rejected():
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize("https:///path/only", settings=_settings())


# ── Port policy ──────────────────────────────────────────────────────
def test_nonstandard_port_rejected_without_a_rule():
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize("https://github.com:8443/r.git", settings=_settings())


def test_nonstandard_port_allowed_only_via_host_rule():
    s = _settings(
        external_git_host_allowlist=[
            {"host": "git.internal", "cidrs": ["10.0.0.0/8"], "ports": [8443]}
        ]
    )
    c = v.parse_and_canonicalize("https://git.internal:8443/r.git", settings=s)
    assert c.port == 8443 and c.canonical_url == "https://git.internal:8443/r.git"
    # A port the rule does NOT list is still rejected.
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize("https://git.internal:9999/r.git", settings=s)


@pytest.mark.parametrize("bad", ["https://github.com:0/r.git", "https://github.com:99999/r.git"])
def test_invalid_port_is_rejected(bad):
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize(bad, settings=_settings())


# ── IP classification (literals, no DNS) ─────────────────────────────
@pytest.mark.parametrize(
    "ip,reason",
    [
        ("127.0.0.1", "loopback"),
        ("127.5.5.5", "loopback"),
        ("::1", "loopback"),
        ("169.254.10.10", "link-local"),
        ("fe80::1", "link-local"),
        ("10.1.2.3", "private"),
        ("172.16.5.5", "private"),
        ("192.168.1.1", "private"),
        ("fc00::1", "unique-local"),
        ("100.64.1.1", "cgnat"),
        ("224.0.0.1", "multicast"),
        ("ff02::1", "multicast"),
        ("0.0.0.0", "unspecified"),
        ("0.1.2.3", "unspecified"),
        ("198.18.0.1", "benchmarking"),
        ("::ffff:127.0.0.1", "loopback"),   # IPv4-mapped → embedded v4 loopback
        ("::ffff:10.0.0.1", "private"),      # IPv4-mapped → embedded v4 private
    ],
)
def test_non_routable_ip_literals_are_rejected(ip, reason):
    with pytest.raises(v.ExternalGitPolicyError) as exc:
        v.resolve_and_check_host(ip, settings=_settings())
    assert reason in str(exc.value)


@pytest.mark.parametrize("ip", [_PUBLIC_V4, _PUBLIC_V6])
def test_global_unicast_literals_are_accepted(ip):
    assert v.resolve_and_check_host(ip, settings=_settings()) == [ip]


# ── IPv4-in-IPv6 encapsulation rejected OUTRIGHT (defense-in-depth) ──
@pytest.mark.parametrize(
    "ip",
    [
        "::ffff:8.8.8.8",           # IPv4-mapped, embedded v4 is GLOBAL
        "::ffff:140.82.121.3",      # IPv4-mapped, embedded v4 is GLOBAL
        "2002:0808:0808::1",        # 6to4 embedding 8.8.8.8 (global)
        "2002:8c52:7903::1",        # 6to4 embedding a public v4
    ],
)
def test_ipv4_encapsulation_is_rejected_even_when_embedded_is_global(ip):
    # These forms are refused regardless of the embedded v4's routability —
    # legit git hosts never need them.
    with pytest.raises(v.ExternalGitPolicyError) as exc:
        v.resolve_and_check_host(ip, settings=_settings())
    assert "encapsulation" in str(exc.value) or "non-routable" in str(exc.value)


def test_teredo_address_is_rejected():
    # Teredo 2001:0::/32 — a documented Teredo literal must be refused.
    with pytest.raises(v.ExternalGitPolicyError):
        v.resolve_and_check_host(
            "2001:0000:4136:e378:8000:63bf:3fff:fdd2", settings=_settings()
        )


def test_encapsulation_rejected_before_allowlist_rule():
    # Even a (deliberately broad) allowlist rule cannot admit an encapsulation
    # form — the encapsulation check runs BEFORE the CIDR allow.
    s = _settings(
        external_git_host_allowlist=[
            {"host": "git.internal", "cidrs": ["::/0"], "ports": [443]}
        ]
    )
    resolver = _FakeResolver({"git.internal": ["::ffff:8.8.8.8"]})
    with pytest.raises(v.ExternalGitPolicyError):
        v.resolve_and_check_host("git.internal", settings=s, resolver=resolver)


def test_encapsulation_of_nonglobal_v4_keeps_specific_reason():
    # The embedded non-global reason is preserved for a precise message.
    with pytest.raises(v.ExternalGitPolicyError) as exc:
        v.resolve_and_check_host("::ffff:169.254.1.1", settings=_settings())
    assert "link-local" in str(exc.value)


# ── Resolution is authoritative (decimal/hex/mixed) ──────────────────
@pytest.mark.parametrize("host", ["2130706433", "0x7f000001", "0177.0.0.1"])
def test_numeric_host_forms_are_caught_by_resolution(host):
    # The pure parser accepts these as opaque names; a resolver expands them to
    # 127.0.0.1 (inet_aton semantics) and the resolved address is authoritative.
    resolver = _FakeResolver({host: ["127.0.0.1"]})
    with pytest.raises(v.ExternalGitPolicyError):
        v.resolve_and_check_host(host, settings=_settings(), resolver=resolver)


def test_every_resolved_address_must_pass():
    # A host that resolves to one public AND one private IP is rejected.
    resolver = _FakeResolver({"mix.example": [_PUBLIC_V4, "10.0.0.1"]})
    with pytest.raises(v.ExternalGitPolicyError):
        v.resolve_and_check_host("mix.example", settings=_settings(), resolver=resolver)


def test_resolve_returns_all_validated_ips():
    resolver = _FakeResolver({"multi.example": [_PUBLIC_V4, "8.8.8.8"]})
    assert v.resolve_and_check_host(
        "multi.example", settings=_settings(), resolver=resolver
    ) == [_PUBLIC_V4, "8.8.8.8"]


# ── (host, CIDR, ports) internal exception ───────────────────────────
def test_allowlisted_host_inside_cidr_is_accepted():
    s = _settings(
        external_git_host_allowlist=[
            {"host": "git.internal", "cidrs": ["10.0.0.0/8"], "ports": [8443]}
        ]
    )
    resolver = _FakeResolver({"git.internal": ["10.4.5.6"]})
    assert v.resolve_and_check_host("git.internal", settings=s, resolver=resolver) == ["10.4.5.6"]


def test_allowlisted_host_resolving_outside_cidr_is_rejected():
    # host matches a rule but resolves outside the rule's CIDRs → reject. A
    # host-only match never exempts the address check.
    s = _settings(
        external_git_host_allowlist=[
            {"host": "git.internal", "cidrs": ["10.0.0.0/8"], "ports": [8443]}
        ]
    )
    resolver = _FakeResolver({"git.internal": ["192.168.1.1"]})
    with pytest.raises(v.ExternalGitPolicyError):
        v.resolve_and_check_host("git.internal", settings=s, resolver=resolver)


def test_allowlisted_host_standard_port_must_be_enumerated_and_pins_to_cidr():
    # An internal-exception host does NOT auto-allow the scheme's
    # standard port — 443 must be listed explicitly. Once listed, the private IP
    # is admitted only because it falls inside the rule's CIDR.
    s = _settings(
        external_git_host_allowlist=[
            {"host": "git.internal", "cidrs": ["10.0.0.0/8"], "ports": [443]}
        ]
    )
    c = v.parse_and_canonicalize("https://git.internal/r.git", settings=s)
    assert c.port == 443
    resolver = _FakeResolver({"git.internal": ["10.9.9.9"]})
    assert v.resolve_and_check_host("git.internal", settings=s, resolver=resolver) == ["10.9.9.9"]


def test_allowlisted_host_standard_port_rejected_when_ports_omit_it():
    # A rule that lists only a custom port does NOT implicitly admit 443.
    s = _settings(
        external_git_host_allowlist=[
            {"host": "git.internal", "cidrs": ["10.0.0.0/8"], "ports": [8443]}
        ]
    )
    with pytest.raises(v.ExternalGitPolicyError):
        v.parse_and_canonicalize("https://git.internal/r.git", settings=s)


# ── Cluster-internal deny ────────────────────────────────────────────
@pytest.mark.parametrize(
    "host",
    ["localhost", "foo.svc", "svc.cluster.local", "app.ns.svc.cluster.local", "db.cluster.local"],
)
def test_cluster_internal_names_are_denied(host):
    # Denied up front regardless of what they would resolve to — even a public
    # CIDR behind the suffix is refused.
    resolver = _FakeResolver({host: [_PUBLIC_V4]})
    with pytest.raises(v.ExternalGitPolicyError):
        v.resolve_and_check_host(host, settings=_settings(), resolver=resolver)


def test_operator_configured_cluster_suffix_is_honored():
    s = _settings(external_git_cluster_dns_suffixes=[".internal.corp"])
    resolver = _FakeResolver({"git.internal.corp": [_PUBLIC_V4]})
    with pytest.raises(v.ExternalGitPolicyError):
        v.resolve_and_check_host("git.internal.corp", settings=s, resolver=resolver)


# ── Branch validation ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "branch", ["main", "master", "develop", "release/1.2.3", "feature/foo-bar", "v1.0.0", "a.b.c"]
)
def test_valid_branches_pass(branch):
    assert v.validate_branch(branch) == branch


@pytest.mark.parametrize(
    "branch",
    [
        "-x",                 # leading dash → parsed as an option
        "--upload-pack=cmd",  # leading dash → parsed as an option
        "@{-1}",              # previous-checkout shorthand
        "foo@{-1}",           # @{-n} shorthand
        "HEAD@{0}",           # reflog shorthand
        "foo@{upstream}",     # upstream shorthand
        "foo..bar",           # parent-dir segment
        "foo//bar",           # empty component
        "foo/",               # trailing slash
        "/foo",               # leading slash
        "foo.lock",           # .lock suffix
        "a/b.lock",           # .lock on a component
        "foo.",               # trailing dot
        ".foo",               # component starts with dot
        "foo/.bar",           # component starts with dot
        "foo@bar",            # '@' outside allowlist charset
        "foo~1",
        "foo^",
        "foo:bar",
        "foo?",
        "foo*",
        "foo[",
        "foo bar",            # space
        "foo\tbar",           # control
        "",                   # empty
        "x" * 300,            # too long
    ],
)
def test_invalid_branches_are_rejected(branch):
    with pytest.raises(v.ExternalGitPolicyError):
        v.validate_branch(branch)


# ── auth_token shape ─────────────────────────────────────────────────
def _base_dict(**extra):
    return {"url": "https://github.com/o/r.git", **extra}


@pytest.mark.parametrize("token", [None, "ghp_abc123", "x" * 4096])
def test_valid_auth_tokens_pass(token):
    r = v.validate(_base_dict(auth_token=token), settings=_settings(), resolve=False)
    assert r.canonical_url == "https://github.com/o/r.git"


@pytest.mark.parametrize(
    "token", ["tok\nabc", "tok\rabc", "tok\x00abc", "x" * 4097, 123, True, ["list"]]
)
def test_invalid_auth_tokens_are_rejected(token):
    with pytest.raises(v.ExternalGitPolicyError):
        v.validate(_base_dict(auth_token=token), settings=_settings(), resolve=False)


# ── poll_interval_secs bounds ────────────────────────────────────────
def test_poll_interval_60_accepted_59_rejected():
    assert v.validate(_base_dict(poll_interval_secs=60), settings=_settings(), resolve=False)
    with pytest.raises(v.ExternalGitPolicyError):
        v.validate(_base_dict(poll_interval_secs=59), settings=_settings(), resolve=False)


@pytest.mark.parametrize("bad", [True, False, "60", 60.0, 59, 10**9])
def test_poll_interval_rejects_non_int_and_out_of_range(bad):
    with pytest.raises(v.ExternalGitPolicyError):
        v.validate(_base_dict(poll_interval_secs=bad), settings=_settings(), resolve=False)


def test_poll_interval_absent_is_allowed():
    # Absent → caller applies its default (300); the validator does not require it.
    assert v.validate(_base_dict(), settings=_settings(), resolve=False)


# ── No secret leaks into any error message ───────────────────────────
def test_error_messages_never_contain_url_userinfo_or_token():
    marker_userinfo = "USERINFOSECRET"
    marker_token = "AUTHTOKENSECRET"
    # userinfo path
    with pytest.raises(v.ExternalGitPolicyError) as e1:
        v.validate(
            {"url": f"https://x-access-token:{marker_userinfo}@evil.example/r.git"},  # pragma: allowlist secret
            settings=_settings(),
            resolve=False,
        )
    assert marker_userinfo not in str(e1.value)
    # token path
    with pytest.raises(v.ExternalGitPolicyError) as e2:
        v.validate(
            _base_dict(auth_token=f"tok\n{marker_token}"), settings=_settings(), resolve=False
        )
    assert marker_token not in str(e2.value)


# ── Transient DNS classification ─────────────────────────────────────
class _TimeoutResolver:
    def resolve(self, host: str, *, timeout: float) -> list[str]:
        raise v.ExternalGitTransientError("timed out")


def test_dns_timeout_is_transient_not_policy():
    with pytest.raises(v.ExternalGitTransientError):
        v.resolve_and_check_host("slow.example", settings=_settings(), resolver=_TimeoutResolver())


def test_default_resolver_timeout_raises_transient(monkeypatch):
    # The caller is never pinned past the deadline: a slow getaddrinfo yields a
    # transient error within `timeout`, and the blocked worker is released via
    # the event so the pool can be reclaimed (no test hang).
    release = threading.Event()

    def _slow(*a, **k):
        release.wait(30)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", _slow)
    resolver = v._BoundedThreadPoolResolver(max_workers=2)
    try:
        with pytest.raises(v.ExternalGitTransientError):
            resolver.resolve("slow.example", timeout=0.2)
    finally:
        release.set()


def test_default_resolver_gaierror_raises_transient(monkeypatch):
    def _boom(*a, **k):
        raise socket.gaierror("NXDOMAIN")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    resolver = v._BoundedThreadPoolResolver(max_workers=2)
    with pytest.raises(v.ExternalGitTransientError):
        resolver.resolve("nope.example", timeout=1.0)


# ── Composed validate() ──────────────────────────────────────────────
def test_validate_full_happy_path_populates_pinned_ips():
    resolver = _FakeResolver({"github.com": [_PUBLIC_V4]})
    r = v.validate(
        {"url": "https://github.com/o/r.git", "branch": "dev", "poll_interval_secs": 120},
        settings=_settings(),
        resolve=True,
        resolver=resolver,
    )
    assert r == v.ValidatedRemote(
        canonical_url="https://github.com/o/r.git",
        scheme="https",
        host="github.com",
        port=443,
        pinned_ips=(_PUBLIC_V4,),
        branch="dev",
    )


def test_validate_resolve_false_skips_dns():
    r = v.validate(_base_dict(), settings=_settings(), resolve=False)
    assert r.pinned_ips == ()
    assert r.branch == "main"  # default applied


def test_validate_accepts_db_row_key_spelling():
    r = v.validate(
        {"remote_url": "https://github.com/o/r.git", "remote_branch": "trunk"},
        settings=_settings(),
        resolve=False,
    )
    assert r.canonical_url == "https://github.com/o/r.git" and r.branch == "trunk"


@pytest.mark.parametrize("data", ["not-a-dict", 42, None, ["url"]])
def test_validate_rejects_non_mapping(data):
    with pytest.raises(v.ExternalGitPolicyError):
        v.validate(data, settings=_settings(), resolve=False)


@pytest.mark.parametrize("url", [123, None, True, {"nested": "x"}])
def test_validate_rejects_non_string_url(url):
    with pytest.raises(v.ExternalGitPolicyError):
        v.validate({"url": url}, settings=_settings(), resolve=False)


def test_validate_rejects_non_global_resolution_end_to_end():
    # A benign-looking public hostname whose DNS resolves to loopback is
    # rejected on the resolved address, not the name.
    resolver = _FakeResolver({"rebind.example": ["127.0.0.1"]})
    with pytest.raises(v.ExternalGitPolicyError):
        v.validate(
            {"url": "https://rebind.example/r.git"},
            settings=_settings(),
            resolve=True,
            resolver=resolver,
        )


# ══ Operator pod/service deny-CIDRs (before the allowlist) ══
def test_deny_cidr_refused_before_allowlist():
    # The allowlist CIDR would admit the IP, but it is ALSO in a pod/service
    # deny-CIDR → refused (deny wins, applied before the allowlist).
    s = _settings(
        external_git_host_allowlist=[
            {"host": "git.internal", "cidrs": ["10.0.0.0/8"], "ports": [443]}
        ],
        external_git_deny_cidrs=["10.42.0.0/16"],
    )
    resolver = _FakeResolver({"git.internal": ["10.42.1.2"]})
    with pytest.raises(v.ExternalGitPolicyError) as exc:
        v.resolve_and_check_host("git.internal", settings=s, resolver=resolver)
    assert "denied" in str(exc.value) or "pod/service" in str(exc.value)


def test_allowlisted_ip_outside_deny_cidr_is_accepted():
    s = _settings(
        external_git_host_allowlist=[
            {"host": "git.internal", "cidrs": ["10.0.0.0/8"], "ports": [443]}
        ],
        external_git_deny_cidrs=["10.42.0.0/16"],
    )
    resolver = _FakeResolver({"git.internal": ["10.9.9.9"]})
    assert v.resolve_and_check_host("git.internal", settings=s, resolver=resolver) == ["10.9.9.9"]


# ══ '=' in the URL path is rejected (config-injection safety) ══
def test_url_path_with_equals_is_rejected():
    with pytest.raises(v.ExternalGitPolicyError) as exc:
        v.parse_and_canonicalize("https://github.com/o/r=epo.git", settings=_settings())
    assert "=" in str(exc.value) or "path" in str(exc.value)


# ══ Host-rule config guards ════════════════════════════════════════════
def test_host_rule_requires_at_least_one_port():
    with pytest.raises(Exception):
        Settings(
            external_git_host_allowlist=[
                {"host": "h.internal", "cidrs": ["10.0.0.0/8"], "ports": []}
            ]
        )


def test_invalid_deny_cidr_rejected_at_config_load():
    with pytest.raises(Exception):
        Settings(external_git_deny_cidrs=["not-a-cidr"])


# ══ Bounded resolver — concurrency cap + no caller hang ═════
def test_bounded_resolver_caps_concurrency(monkeypatch):
    gate = threading.Event()
    entered = threading.Semaphore(0)

    def _slow(*a, **k):
        entered.release()
        gate.wait(20)
        return [(0, 0, 0, "", ("1.2.3.4", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _slow)
    resolver = v._BoundedThreadPoolResolver(max_workers=2)
    results: list = []

    def worker():
        try:
            results.append(resolver.resolve("h", timeout=10))
        except v.ExternalGitTransientError:
            results.append(None)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        assert entered.acquire(timeout=5)       # 1st getaddrinfo started
        assert entered.acquire(timeout=5)       # 2nd started
        assert not entered.acquire(timeout=1)   # 3rd BLOCKED — cap of 2 enforced
    finally:
        gate.set()
    for t in threads:
        t.join(15)
    assert results.count(["1.2.3.4"]) == 4      # cap gated, did not drop work


def test_bounded_resolver_timeout_does_not_accumulate_or_hang(monkeypatch):
    # Repeated timeouts against a wedged resolver never hang the caller and never
    # grow the pool past its cap (the fix for the abandoned-daemon-thread leak).
    gate = threading.Event()
    resolver = v._BoundedThreadPoolResolver(max_workers=2)

    def _wedged(*a, **k):
        gate.wait(20)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", _wedged)
    try:
        for _ in range(6):
            with pytest.raises(v.ExternalGitTransientError):
                resolver.resolve("wedged.example", timeout=0.1)
        assert resolver._max_workers == 2
    finally:
        gate.set()


def test_bounded_resolver_backlog_stays_bounded_when_workers_wedged(monkeypatch):
    # Regression: with the pool saturated by a wedged getaddrinfo, further
    # resolve() calls MUST NOT keep submitting work.
    # The old "submit always, cancel the future on timeout" scheme left cancelled
    # items stuck in ThreadPoolExecutor's internal queue (reproduced with
    # max_workers=1, 4 consecutive timeouts → 3 queued work items) — an
    # unbounded-memory growth bug and a real breach of
    # external_git_max_concurrent_resolutions. The capacity
    # semaphore caps submitted-but-unfinished work (running + queued) at the pool
    # size; a caller that cannot reserve a slot gets ExternalGitTransientError
    # and never enqueues.
    cap = 1
    started = threading.Event()
    gate = threading.Event()
    submits = 0

    def _wedged(*a, **k):
        started.set()
        gate.wait(20)
        return [(0, 0, 0, "", ("1.2.3.4", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _wedged)
    resolver = v._BoundedThreadPoolResolver(max_workers=cap)

    # Count how many work items actually reach the pool (no real DNS is done).
    real_submit = resolver._executor.submit

    def _counting_submit(fn, *a, **k):
        nonlocal submits
        submits += 1
        return real_submit(fn, *a, **k)

    monkeypatch.setattr(resolver._executor, "submit", _counting_submit)

    try:
        # 1st call takes the only slot; its getaddrinfo wedges the sole worker.
        with pytest.raises(v.ExternalGitTransientError):
            resolver.resolve("wedged", timeout=0.1)
        assert started.wait(5)  # the worker is really inside getaddrinfo now

        # While that slot is held, a burst of further calls must each fail on the
        # capacity gate WITHOUT submitting — the backlog must not grow.
        for _ in range(4):
            with pytest.raises(v.ExternalGitTransientError):
                resolver.resolve("wedged", timeout=0.05)

        # Invariant running + queued <= cap: only the 1st call ever submitted,
        # and nothing is left waiting in the executor's internal queue (the exact
        # quantity that accumulated under the old code).
        assert submits == cap
        assert resolver._executor._work_queue.qsize() == 0
    finally:
        gate.set()
