"""READ-authority gate for the `if_not_exists` no-op projection.

`akb_create_table` is write-scoped and `akb_browse` is read-scoped, and
`token_has_scope` is `"admin" in granted or required in granted` — there is no
write->read implication. So a write-only credential can create tables and
cannot browse, and the enriched no-op envelope (URI, collection, columns, keys,
indexes, and the `matches_request`/`mismatches` schema oracles) would be a new
disclosure to it.

The trap this file exists for: `token_has_scope(None, ...)` returns True by
design — `None` means "unscoped credential", i.e. a JWT login. But an OAuth
credential ALSO carries `token_scopes=None` and puts its grants in
`oauth_scopes` instead. A gate that inspects only `token_scopes` therefore
waves every OAuth token through, including one holding nothing but
`akb:vault:write`.
"""

from __future__ import annotations

import tempfile

import pytest

from app.config import settings

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module", autouse=True)
def _temp_git_storage_path():
    """Point `git_storage_path` at a temp dir for this module only.

    `mcp_server.server` constructs a DocumentService (and therefore a
    GitService) at import time, which mkdirs this path. The import is lazy —
    it happens inside each test — so setting it here, before `yield`, is
    early enough.

    Deliberately NOT a module-level statement: that mutates during
    COLLECTION, so `--collect-only`, full deselection, or a collection error
    would leave the process-global setting pointing at a temp dir with no
    test ever running to restore it.

    Known limitation, pre-existing and shared with `test_mcp_oauth_unit`:
    `mcp_server.server` caches a module-level `doc_service`, whose GitService
    captures this path at construction. Restoring the setting does not
    reconstruct that object, so a later test importing the same module still
    sees the temp path. Fixing that means changing how the module is imported
    under test, which is outside this change.
    """
    original = settings.git_storage_path
    try:
        settings.git_storage_path = tempfile.mkdtemp(
            prefix="akb-read-authority-test-")
        yield
    finally:
        settings.git_storage_path = original


class _User:
    def __init__(self, *, token_scopes=None, oauth_scopes=None):
        self.user_id = "u1"
        self.username = "u"
        self.token_scopes = token_scopes
        self.oauth_scopes = oauth_scopes


def _allow_vault(monkeypatch, module, ok: bool = True):
    async def _check(*a, **k):
        if not ok:
            raise PermissionError("no reader role")
        return {"vault_id": "v1"}

    monkeypatch.setattr(module, "check_vault_access", _check)


# ── the OAuth trap, on both surfaces ─────────────────────────────


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_oauth_write_only_token_is_denied_read_authority(monkeypatch, surface):
    """An OAuth token holding only `akb:vault:write` must NOT be granted the
    enriched projection. It reaches this code because writing is what it is
    allowed to do; reading is not."""
    if surface == "rest":
        from app.api.routes import tables as mod
        _allow_vault(monkeypatch, mod)
        got = await mod._can_read_vault(
            _User(oauth_scopes=["akb:vault:write"]), "v")
    else:
        from mcp_server import server as mod
        _allow_vault(monkeypatch, mod)
        got = await mod._can_read_vault(
            _User(oauth_scopes=["akb:vault:write"]), "u1", "v")
    assert got is False


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_oauth_token_with_read_scope_is_granted(monkeypatch, surface):
    if surface == "rest":
        from app.api.routes import tables as mod
        _allow_vault(monkeypatch, mod)
        got = await mod._can_read_vault(
            _User(oauth_scopes=["akb:vault:read", "akb:vault:write"]), "v")
    else:
        from mcp_server import server as mod
        _allow_vault(monkeypatch, mod)
        got = await mod._can_read_vault(
            _User(oauth_scopes=["akb:vault:read", "akb:vault:write"]),
            "u1", "v")
    assert got is True


# ── PAT scopes ───────────────────────────────────────────────────


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_pat_write_only_is_denied(monkeypatch, surface):
    if surface == "rest":
        from app.api.routes import tables as mod
        _allow_vault(monkeypatch, mod)
        got = await mod._can_read_vault(
            _User(token_scopes=frozenset({"write"})), "v")
    else:
        from mcp_server import server as mod
        _allow_vault(monkeypatch, mod)
        got = await mod._can_read_vault(
            _User(token_scopes=frozenset({"write"})), "u1", "v")
    assert got is False


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_unscoped_jwt_login_is_granted(monkeypatch, surface):
    """`token_scopes=None` AND `oauth_scopes=None` is a plain JWT login —
    genuinely unscoped, so scope is not the thing that limits it. Vault ACL
    still does."""
    if surface == "rest":
        from app.api.routes import tables as mod
        _allow_vault(monkeypatch, mod)
        got = await mod._can_read_vault(_User(), "v")
    else:
        from mcp_server import server as mod
        _allow_vault(monkeypatch, mod)
        got = await mod._can_read_vault(_User(), "u1", "v")
    assert got is True


# ── the flag must be a real boolean ──────────────────────────────


@pytest.mark.parametrize("bad", ["true", "false", 1, 0, "yes", []])
async def test_non_boolean_if_not_exists_is_rejected(monkeypatch, bad):
    """Silently coercing is worse than either accepting or rejecting.
    `bool("false")` is True, so a lenient cast hands the caller idempotent
    behaviour it never asked for; a strict `is True` hands `"true"` the
    409 it did not expect. Reject, so the caller learns."""
    from mcp_server import server as mod

    out = await mod._handle_create_table(
        {"vault": "v", "name": "t", "columns": [], "if_not_exists": bad},
        "u1", _User(),
    )
    assert out.get("error"), f"{bad!r} should have been rejected, got {out}"
    assert "if_not_exists" in str(out)


async def test_absent_if_not_exists_is_still_accepted(monkeypatch):
    """Omission is not a malformed value — it is the documented default."""
    from mcp_server import server as mod

    _allow_vault(monkeypatch, mod)

    called = {}

    async def _svc(*a, **k):
        called.update(k)
        return {"kind": "table", "created": True}

    monkeypatch.setattr(mod.table_service, "create_table", _svc)
    monkeypatch.setattr(mod, "_can_read_vault", lambda *a, **k: _true())

    out = await mod._handle_create_table(
        {"vault": "v", "name": "t", "columns": []}, "u1", _User())
    assert not out.get("error"), out
    assert called["if_not_exists"] is False


async def _true():
    return True


# ── the vault ACL still applies, and failures fail closed ────────


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_reader_role_denied_means_no_read_authority(monkeypatch, surface):
    if surface == "rest":
        from app.api.routes import tables as mod
        _allow_vault(monkeypatch, mod, ok=False)
        got = await mod._can_read_vault(_User(), "v")
    else:
        from mcp_server import server as mod
        _allow_vault(monkeypatch, mod, ok=False)
        got = await mod._can_read_vault(_User(), "u1", "v")
    assert got is False
