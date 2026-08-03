"""Layer-1 (create-time) external-git validation + canonical MCP response.

Two contracts are pinned here:

1. **Create-path rejection is a pure 422 with ZERO side effects.** An invalid,
   malformed, or disabled external-git remote must reject BEFORE ``create_vault``
   touches any repo / pool / git — no vault row, no ``vault_external_git``
   sidecar, no poller claim. Proven with a *hollow* ``DocumentService`` built via
   ``object.__new__`` (no ``__init__``, so no ``.git`` / repos): if validation
   did not run first, the call would ``AttributeError`` / hit the DB instead of
   raising ``ValidationError``. None of these cases perform DNS (IP literals +
   pure shape/scheme/branch/poll errors), so no network and no resolver mock is
   needed for the rejection matrix.

2. **What is PERSISTED and ECHOED is the canonical URL, never the raw input.**
   The mirror INSERT stores ``validated.canonical_url`` (host lowercased, default
   port dropped, userinfo rejected) with the token in the separate column, and
   the MCP create response re-reads that stored row. The one DNS leg
   in the happy paths is exercised with an injected fake resolver — never a real
   lookup (no real network).
"""

from __future__ import annotations

import tempfile
import uuid

import pytest

from app.config import settings

# DocumentService()/GitService() touch the git storage path on construction;
# point it somewhere writable before importing modules that build one (same
# idiom as test_vault_create_scope_unit.py).
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-extgit-create-test-")

from app.exceptions import ValidationError  # noqa: E402
from app.models.vault_scope import current_vault_scope  # noqa: E402
from app.services import document_service as ds  # noqa: E402
from app.services import external_git_validation as egv  # noqa: E402
from app.services.document_service import DocumentService  # noqa: E402
from app.util.errors import INVALID_ARGUMENT  # noqa: E402

_PUBLIC_V4 = "140.82.121.3"  # a definitely-global address for happy paths


# ── Test doubles ─────────────────────────────────────────────────────
class _FakeResolver:
    """Deterministic resolver: ``mapping`` gives host -> [ip, ...]; unlisted
    hosts resolve to a single global public address so happy paths need no
    entry. Injected in place of the bounded real resolver — no network."""

    def __init__(self, mapping: dict[str, list[str]] | None = None) -> None:
        self.mapping = mapping or {}

    def resolve(self, host: str, *, timeout: float) -> list[str]:
        return list(self.mapping.get(host, [_PUBLIC_V4]))


class _AsyncCtx:
    def __init__(self, value=None):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def transaction(self):
        return _AsyncCtx()


class _FakePool:
    def acquire(self):
        return _AsyncCtx(_FakeConn())


async def _fake_get_pool():
    return _FakePool()


class _StubUser:
    """Only ``.user_id`` / ``.username`` are read on the create path."""

    def __init__(self) -> None:
        self.user_id = "00000000-0000-0000-0000-000000000001"
        self.username = "creator"


# ── 1. Rejection matrix — pure 422, zero side effects (hollow instance) ──
# Each case rejects WITHOUT DNS and WITHOUT any repo/pool/git access.
_INVALID_REMOTES = {
    "scheme-file": {"url": "file:///etc/passwd"},
    "scheme-git": {"url": "git://github.com/o/r.git"},
    "scheme-ssh": {"url": "ssh://git@github.com/o/r.git"},
    "ext-transport": {"url": "ext::sh -c whoami"},
    "userinfo-pat": {"url": "https://x-access-token:tok@github.com/o/r.git"},  # pragma: allowlist secret
    "userinfo-basic": {"url": "https://user:pass@github.com/o/r.git"},  # pragma: allowlist secret
    "nonglobal-linklocal": {"url": "https://169.254.169.254/o/r.git"},
    "nonglobal-loopback-v4": {"url": "https://127.0.0.1/o/r.git"},
    "nonglobal-loopback-v6": {"url": "https://[::1]/o/r.git"},
    "nonglobal-rfc1918": {"url": "https://10.0.0.5/o/r.git"},
    "nonglobal-cgnat": {"url": "https://100.64.1.1/o/r.git"},
    "branch-shorthand": {"url": "https://github.com/o/r.git", "branch": "@{-1}"},
    "branch-leadingdash": {"url": "https://github.com/o/r.git", "branch": "-x"},
    "branch-dotdot": {"url": "https://github.com/o/r.git", "branch": "a/../b"},
    "poll-bool": {"url": "https://github.com/o/r.git", "poll_interval_secs": True},
    "poll-too-small": {"url": "https://github.com/o/r.git", "poll_interval_secs": 10},
    "poll-too-large": {"url": "https://github.com/o/r.git", "poll_interval_secs": 99_999_999},
    "no-url": {},
    "url-not-str": {"url": 123},
    "query-string": {"url": "https://github.com/o/r.git?foo=bar"},
    "control-char": {"url": "https://github.com/o/r\n.git"},
}


@pytest.mark.parametrize("cfg", list(_INVALID_REMOTES.values()), ids=list(_INVALID_REMOTES))
async def test_create_vault_rejects_invalid_remote_with_zero_side_effects(cfg):
    # Hollow instance: no __init__, so no `.git`/repos. Reaching `_repos()`
    # (past validation) would AttributeError/hit the DB, not raise
    # ValidationError — so a clean ValidationError proves validation ran FIRST.
    svc = object.__new__(DocumentService)
    with pytest.raises(ValidationError):
        await svc.create_vault("mirror-vault", external_git=cfg)


async def test_create_vault_rejects_non_dict_external_git():
    svc = object.__new__(DocumentService)
    with pytest.raises(ValidationError):
        await svc.create_vault("mirror-vault", external_git="not-a-dict")  # type: ignore[arg-type]


async def test_reject_message_never_leaks_the_raw_url_or_token():
    # A policy reject names the violation class only — never the raw
    # URL, its userinfo, or the token.
    svc = object.__new__(DocumentService)
    with pytest.raises(ValidationError) as exc:
        await svc.create_vault(
            "mirror-vault",
            external_git={"url": "https://s3cr3t-token:p4ss@169.254.169.254/o/r.git"},  # pragma: allowlist secret
        )
    msg = str(exc.value)
    assert "s3cr3t-token" not in msg
    assert "p4ss" not in msg
    assert "169.254.169.254" not in msg


# ── 2. Kill-switch ───────────────────────────────────────────────────
async def test_create_vault_refuses_mirror_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "external_git_enabled", False)
    svc = object.__new__(DocumentService)
    with pytest.raises(ValidationError) as exc:
        await svc.create_vault(
            "mirror-vault", external_git={"url": "https://github.com/o/r.git"},
        )
    assert "disabled" in str(exc.value).lower()


async def test_disabled_kill_switch_precedes_any_dns(monkeypatch):
    # Feature off ⇒ reject WITHOUT resolving the target host. A resolver that
    # explodes if touched proves the kill-switch fires before host resolution.
    monkeypatch.setattr(settings, "external_git_enabled", False)

    def _boom(_n):
        raise AssertionError("resolver must not be reached when feature disabled")

    monkeypatch.setattr(egv, "_get_default_resolver", _boom)
    svc = object.__new__(DocumentService)
    with pytest.raises(ValidationError):
        await svc.create_vault(
            "mirror-vault", external_git={"url": "https://needs-dns.example/o/r.git"},
        )


# ── 3. Happy path — canonical URL persisted, token separated ─────────
async def test_create_vault_mirror_persists_canonical_url_not_input(monkeypatch):
    # Resolver -> a global IP so the host-safety check passes without real DNS.
    monkeypatch.setattr(
        egv, "_get_default_resolver",
        lambda n: _FakeResolver({"github.com": [_PUBLIC_V4]}),
    )

    captured: dict = {}
    vault_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111")

    class _VaultRepo:
        async def get_by_name(self, name):
            return None

        async def create(self, name, description, git_path, *, owner_id, public_access, conn=None):
            captured["vault_name"] = name
            return vault_uuid

    async def _fake_repos(self):
        return _VaultRepo(), object(), object()

    async def _cap_egit_create(
        self, *, vault_id, remote_url, remote_branch, auth_token,
        poll_interval_secs, conn=None,
    ):
        captured.update(
            vault_id=vault_id, remote_url=remote_url, remote_branch=remote_branch,
            auth_token=auth_token, poll_interval_secs=poll_interval_secs,
        )

    class _FakeRoleSync:
        async def on_vault_create(self, *a, **k):
            return None

        async def on_public_access_change(self, *a, **k):
            return None

    monkeypatch.setattr(ds.DocumentService, "_repos", _fake_repos)
    monkeypatch.setattr(ds, "get_pool", _fake_get_pool)
    monkeypatch.setattr(ds.VaultExternalGitRepository, "create", _cap_egit_create)
    monkeypatch.setattr(ds, "get_role_sync", lambda: _FakeRoleSync())

    svc = DocumentService()
    vid = await svc.create_vault(
        "mirror-vault",
        # Non-canonical input: uppercase host + explicit default :443.
        external_git={
            "url": "https://GitHub.com:443/OWNER/repo.git",
            "auth_token": "s3cr3t-token",
        },
    )

    assert vid == str(vault_uuid)
    # Canonical, NOT the raw input: host lowercased, default :443 dropped,
    # path case preserved.
    assert captured["remote_url"] == "https://github.com/OWNER/repo.git"
    assert captured["remote_branch"] == "main"           # default, validated
    assert captured["auth_token"] == "s3cr3t-token"      # separate column
    assert captured["poll_interval_secs"] == 300         # create-time default
    assert captured["vault_id"] == vault_uuid


async def test_create_vault_mirror_accepts_custom_branch_and_interval(monkeypatch):
    monkeypatch.setattr(
        egv, "_get_default_resolver", lambda n: _FakeResolver(),
    )
    captured: dict = {}

    class _VaultRepo:
        async def get_by_name(self, name):
            return None

        async def create(self, *a, **k):
            return uuid.UUID("22222222-2222-2222-2222-222222222222")

    async def _fake_repos(self):
        return _VaultRepo(), object(), object()

    async def _cap_egit_create(self, *, remote_branch, poll_interval_secs, **k):
        captured.update(remote_branch=remote_branch, poll_interval_secs=poll_interval_secs)

    class _FakeRoleSync:
        async def on_vault_create(self, *a, **k):
            return None

        async def on_public_access_change(self, *a, **k):
            return None

    monkeypatch.setattr(ds.DocumentService, "_repos", _fake_repos)
    monkeypatch.setattr(ds, "get_pool", _fake_get_pool)
    monkeypatch.setattr(ds.VaultExternalGitRepository, "create", _cap_egit_create)
    monkeypatch.setattr(ds, "get_role_sync", lambda: _FakeRoleSync())

    svc = DocumentService()
    await svc.create_vault(
        "mirror-vault",
        external_git={
            "url": "https://example.com/o/r.git",
            "branch": "release/1.2.3",
            "poll_interval_secs": 3600,
        },
    )
    assert captured["remote_branch"] == "release/1.2.3"
    assert captured["poll_interval_secs"] == 3600


# ── 4. MCP create response echoes the STORED canonical URL ──
async def test_mcp_create_response_echoes_stored_canonical_url(monkeypatch):
    from mcp_server import server
    from app.repositories import vault_external_git_repo as veg_repo

    async def _fake_create_vault(name, description="", **kwargs):
        return "11111111-1111-1111-1111-111111111111"

    async def _fake_get(self, vault_id):
        # The row as PERSISTED by Layer-1: canonical + credential-free.
        return {
            "remote_url": "https://github.com/OWNER/repo.git",
            "remote_branch": "main",
        }

    monkeypatch.setattr(server.doc_service, "create_vault", _fake_create_vault)
    monkeypatch.setattr(veg_repo.VaultExternalGitRepository, "get", _fake_get)
    monkeypatch.setattr(server, "get_pool", _fake_get_pool)

    token = current_vault_scope.set(None)  # unscoped PAT ⇒ owner-scope guard no-op
    try:
        result = await server._handle_create_vault(
            {
                "name": "mirror-vault",
                # Raw input differs from canonical (uppercase host + :443).
                "external_git": {"url": "https://GitHub.com:443/OWNER/repo.git", "branch": "main"},
            },
            "00000000-0000-0000-0000-000000000001",
            _StubUser(),
        )
    finally:
        current_vault_scope.reset(token)

    assert result["vault_id"] == "11111111-1111-1111-1111-111111111111"
    # Canonical stored value, NOT the raw input URL.
    assert result["external_git"]["url"] == "https://github.com/OWNER/repo.git"
    assert result["external_git"]["branch"] == "main"
    assert result["external_git"]["read_only"] is True


async def test_mcp_create_rejects_invalid_remote_as_invalid_argument():
    # End-to-end through the REAL handler + REAL create_vault: a non-global IP
    # literal rejects before any DB write (no stubbing), so the response is an
    # invalid_argument envelope with NO vault_id (zero side effects). No DNS
    # (IP literal short-circuits the resolver), no DB.
    from mcp_server import server

    token = current_vault_scope.set(None)
    try:
        result = await server._handle_create_vault(
            {
                "name": "mirror-vault",
                "external_git": {"url": "https://169.254.169.254/o/r.git"},
            },
            "00000000-0000-0000-0000-000000000001",
            _StubUser(),
        )
    finally:
        current_vault_scope.reset(token)

    assert result.get("code") == INVALID_ARGUMENT
    assert "vault_id" not in result           # nothing created
    assert "169.254.169.254" not in result.get("error", "")  # no target leak
