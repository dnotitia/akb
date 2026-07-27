"""Vault CREATION honours the per-PAT vault scope (dnotitia/akb#284).

The Option B scope guard used to live inline in `check_vault_access`,
which resolves an existing vault row before reaching it — structurally
unable to cover creation, and neither creation entry point called it.
A scoped token could therefore create a vault anywhere in the namespace
and was then refused every administrative operation on the vault it had
just made, including deleting it: creation and administration disagreed
about the same scope, and the asymmetry stranded the vault.

The predicate now lives in `access_service.check_vault_scope` — a pure
name-only check, no DB — which `check_vault_access` calls for existing
vaults and which both creation entry points call directly.

DB-free by construction: every case here is refused (or admitted) before
any repository call, so the service is stubbed and never reached on the
allow path.
"""

from __future__ import annotations

import tempfile

import pytest

from app.config import settings

# Both entry-point modules construct a DocumentService() at import time,
# which mkdir's the git storage path. Point it somewhere writable before
# those imports so they don't try to create the default `/data/vaults`.
# Same idiom as test_mcp_oauth_unit.py.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-vault-create-scope-test-")

from app.exceptions import ForbiddenError  # noqa: E402
from app.models.vault_scope import VaultScope, current_vault_scope  # noqa: E402
from app.services.access_service import check_vault_scope  # noqa: E402

# The scope from the issue's reproduction.
SCOPE = VaultScope(prefixes=("gdn-e2e-", "e2e-gdn-"), extra_vaults=frozenset({"slack-ops"}))

IN_SCOPE = "gdn-e2e-scopeprobe"
OUT_OF_SCOPE = "zz-scope-probe"


@pytest.fixture
def scoped():
    """Run the test body under a PAT that carries SCOPE."""
    token = current_vault_scope.set(SCOPE)
    try:
        yield SCOPE
    finally:
        current_vault_scope.reset(token)


@pytest.fixture
def unscoped():
    """Run the test body under a PAT with no vault scope (NULL column)."""
    token = current_vault_scope.set(None)
    try:
        yield None
    finally:
        current_vault_scope.reset(token)


class _StubUser:
    """Only `.user_id` / `.username` are read on the creation paths."""

    def __init__(self) -> None:
        self.user_id = "00000000-0000-0000-0000-000000000001"
        self.username = "scoped-agent"
        self.is_admin = False


# ── The predicate itself ───────────────────────────────────────────


class TestCheckVaultScope:
    def test_denies_name_outside_scope(self, scoped) -> None:
        with pytest.raises(ForbiddenError) as exc:
            check_vault_scope(OUT_OF_SCOPE, required_role="owner")
        # 403 ⇒ `permission_denied` at both the REST and MCP envelopes.
        assert exc.value.status_code == 403
        assert OUT_OF_SCOPE in str(exc.value)

    def test_permits_name_inside_scope(self, scoped) -> None:
        check_vault_scope(IN_SCOPE, required_role="owner")  # no raise
        check_vault_scope("e2e-gdn-other", required_role="owner")
        check_vault_scope("slack-ops", required_role="owner")  # exact whitelist

    def test_unscoped_token_permits_anything(self, unscoped) -> None:
        check_vault_scope(OUT_OF_SCOPE, required_role="owner")  # no raise

    def test_non_mutating_role_is_never_scope_restricted(self, scoped) -> None:
        # Reads stay unrestricted — a scoped agent still READS broadly.
        check_vault_scope(OUT_OF_SCOPE, required_role="reader")

    def test_message_matches_the_existing_guard_wording(self, scoped) -> None:
        # One predicate, one message: the wording a caller sees on an
        # out-of-scope CREATE is the wording `check_vault_access` has
        # always used for an out-of-scope admin op.
        with pytest.raises(ForbiddenError) as exc:
            check_vault_scope(OUT_OF_SCOPE, required_role="admin")
        assert str(exc.value) == (
            f"Token scope does not permit 'admin' on vault '{OUT_OF_SCOPE}'"
        )


# ── Entry point 1: MCP `akb_create_vault` ──────────────────────────


class TestMcpCreateVault:
    async def test_denies_out_of_scope_create(self, scoped, monkeypatch) -> None:
        from mcp_server import server

        called = []

        async def _record(name, description="", **kwargs):
            called.append(name)
            return "deadbeef-0000-0000-0000-000000000000"

        monkeypatch.setattr(server.doc_service, "create_vault", _record)

        with pytest.raises(ForbiddenError) as exc:
            await server._handle_create_vault(
                {"name": OUT_OF_SCOPE}, "uid", _StubUser(),
            )
        assert OUT_OF_SCOPE in str(exc.value)
        # Guard-first: the service is never reached, so nothing is created.
        assert called == []

    async def test_permits_in_scope_create(self, scoped, monkeypatch) -> None:
        from mcp_server import server

        async def _fake_create_vault(name, description="", **kwargs):
            return "11111111-1111-1111-1111-111111111111"

        monkeypatch.setattr(server.doc_service, "create_vault", _fake_create_vault)

        result = await server._handle_create_vault(
            {"name": IN_SCOPE}, "uid", _StubUser(),
        )
        assert result["name"] == IN_SCOPE
        assert result["vault_id"] == "11111111-1111-1111-1111-111111111111"

    async def test_unscoped_token_creates_anywhere(self, unscoped, monkeypatch) -> None:
        # No-regression control: a PAT with a NULL vault_scope keeps the
        # historical full-ACL behaviour.
        from mcp_server import server

        async def _fake_create_vault(name, description="", **kwargs):
            return "22222222-2222-2222-2222-222222222222"

        monkeypatch.setattr(server.doc_service, "create_vault", _fake_create_vault)

        result = await server._handle_create_vault(
            {"name": OUT_OF_SCOPE}, "uid", _StubUser(),
        )
        assert result["name"] == OUT_OF_SCOPE


# ── Entry point 2: REST `POST /vaults` ─────────────────────────────


class TestRestCreateVault:
    async def test_denies_out_of_scope_create(self, scoped, monkeypatch) -> None:
        from app.api.routes import documents

        called = []

        async def _record(name, description="", **kwargs):
            called.append(name)
            return "deadbeef-0000-0000-0000-000000000000"

        monkeypatch.setattr(documents.doc_service, "create_vault", _record)

        with pytest.raises(ForbiddenError) as exc:
            await documents.create_vault(name=OUT_OF_SCOPE, user=_StubUser())
        assert OUT_OF_SCOPE in str(exc.value)
        assert called == []

    async def test_permits_in_scope_create(self, scoped, monkeypatch) -> None:
        from app.api.routes import documents

        async def _fake_create_vault(name, description="", **kwargs):
            return "33333333-3333-3333-3333-333333333333"

        monkeypatch.setattr(documents.doc_service, "create_vault", _fake_create_vault)

        result = await documents.create_vault(name=IN_SCOPE, user=_StubUser())
        assert result["name"] == IN_SCOPE
        assert result["vault_id"] == "33333333-3333-3333-3333-333333333333"

    async def test_unscoped_token_creates_anywhere(self, unscoped, monkeypatch) -> None:
        from app.api.routes import documents

        async def _fake_create_vault(name, description="", **kwargs):
            return "44444444-4444-4444-4444-444444444444"

        monkeypatch.setattr(documents.doc_service, "create_vault", _fake_create_vault)

        result = await documents.create_vault(name=OUT_OF_SCOPE, user=_StubUser())
        assert result["name"] == OUT_OF_SCOPE

    async def test_scope_is_checked_on_the_normalised_name(
        self, scoped, monkeypatch,
    ) -> None:
        # The route NFC-normalises before storing; the guard must see the
        # same string the service will, so normalisation can't shift a
        # name across the scope boundary after the check.
        from app.api.routes import documents

        seen = []

        async def _fake_create_vault(name, description="", **kwargs):
            seen.append(name)
            return "55555555-5555-5555-5555-555555555555"

        monkeypatch.setattr(documents.doc_service, "create_vault", _fake_create_vault)

        await documents.create_vault(name=IN_SCOPE, user=_StubUser())
        assert seen == [IN_SCOPE]


# ── Regression boundary ────────────────────────────────────────────


class TestExistingSurfacesUnchanged:
    """`check_vault_access` kept its behaviour when the shared predicate
    was extracted out of it — same roles gated, same wording, and the
    guard still sits ahead of the is_admin / owner short-circuits."""

    def test_check_vault_access_still_calls_the_predicate(self) -> None:
        import inspect

        from app.services import access_service

        src = inspect.getsource(access_service.check_vault_access)
        assert "check_vault_scope(vault_name, required_role)" in src

    def test_scope_gate_still_covers_exactly_the_mutating_roles(self, scoped) -> None:
        from app.services.access_service import _MUTATING_ROLES

        assert _MUTATING_ROLES == frozenset({"writer", "admin", "owner"})
        for role in sorted(_MUTATING_ROLES):
            with pytest.raises(ForbiddenError):
                check_vault_scope(OUT_OF_SCOPE, required_role=role)
        for role in ("reader", "none", ""):
            check_vault_scope(OUT_OF_SCOPE, required_role=role)  # no raise

    def test_in_scope_vault_is_untouched_for_every_role(self, scoped) -> None:
        # Administration of an in-scope vault must not have become
        # stricter — the fix removes an asymmetry, it doesn't add one.
        for role in ("reader", "writer", "admin", "owner"):
            check_vault_scope(IN_SCOPE, required_role=role)
