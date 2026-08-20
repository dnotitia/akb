"""Unit tests for akb_help(topic='vault-skill', vault?) routing."""
import asyncio

import pytest

from app.services.skill_policy import VAULT_SKILL_PATH
from mcp_server import help as help_mod
from mcp_server.help import (
    VAULT_SKILL_TOPIC_BODY,
    render_vault_skill_response,
)


def test_topic_body_constant_exists():
    """The static topic body explains the system-managed convention."""
    assert "vault-skill" in VAULT_SKILL_TOPIC_BODY.lower()
    # The skill is seeded and guarded — never created by the agent/owner.
    assert "akb_put" not in VAULT_SKILL_TOPIC_BODY
    assert "akb_update" in VAULT_SKILL_TOPIC_BODY
    assert "vault_skill" in VAULT_SKILL_TOPIC_BODY  # names the payload key


def test_canonical_path_single_source():
    """help.py re-exports the policy constant rather than redefining it."""
    assert help_mod.VAULT_SKILL_PATH is VAULT_SKILL_PATH
    assert VAULT_SKILL_PATH == "overview/vault-skill.md"


def test_render_with_vault_present():
    """When the doc exists, return body verbatim with source attribution."""
    async def fake_fetch(vault, doc_id):
        return {
            "content": "# My vault skill\n\nCustom rules here.",
            "commit": "abc1234",
            "updated_at": "2026-05-14T10:00:00Z",
        }

    out = asyncio.run(render_vault_skill_response(vault="my-vault", fetch_fn=fake_fetch))
    assert "# Vault skill for my-vault" in out
    assert "<!-- akb-skill-source -->" in out
    assert "Source: vault owner" in out
    assert "Custom rules here." in out


def test_render_with_vault_missing():
    """When the doc is missing, return the mirror-vault notice + fallback rules."""
    async def fake_fetch(vault, doc_id):
        return None  # sentinel: doc not found

    out = asyncio.run(render_vault_skill_response(vault="empty-vault", fetch_fn=fake_fetch))
    assert "# Vault skill for empty-vault" in out
    assert "overview/vault-skill.md" in out
    assert "akb_browse" in out  # fallback onboarding steps
    assert "${{secrets.X}}" in out  # secrets fallback


@pytest.mark.asyncio
async def test_missing_branch_no_put_instruction():
    """The missing branch must not hand out an unfollowable akb_put recipe."""
    async def fetch(v, d):
        return None

    out = await help_mod.render_vault_skill_response("mirror-v", fetch)
    assert "akb_put(" not in out
    assert "mirror" in out.lower() or "read-only" in out.lower()


def test_render_without_vault_arg():
    """When no vault arg, returns just the static topic body."""
    out = asyncio.run(render_vault_skill_response(vault=None, fetch_fn=None))
    assert out == VAULT_SKILL_TOPIC_BODY


# ── Access gate on the vault-specific branch ──────────────────────────
# The branch serves a vault's authored content, so it is a vault read. Before
# the gate it served any vault's skill to any authenticated caller.


@pytest.mark.asyncio
async def test_vault_branch_requires_reader(monkeypatch):
    import tempfile

    from app.config import settings
    settings.git_storage_path = tempfile.mkdtemp(prefix="akb-help-skill-test-")
    from app.exceptions import ForbiddenError
    from mcp_server import server as server_mod

    checked: list[tuple] = []

    async def deny(uid, vault, required_role="reader", **kw):
        checked.append((uid, vault, required_role))
        raise ForbiddenError("nope")

    async def _never(*a, **k):
        raise AssertionError("access check must fire before the document read")

    monkeypatch.setattr(server_mod, "check_vault_access", deny)
    monkeypatch.setattr(server_mod.doc_service, "get", _never)

    handler = server_mod._HANDLERS["akb_help"]
    with pytest.raises(ForbiddenError):
        await handler(
            {"topic": "vault-skill", "vault": "not-mine"},
            "u1",
            server_mod._MCPUser(),
        )
    assert checked == [("u1", "not-mine", "reader")]


@pytest.mark.asyncio
async def test_static_topic_never_access_checks(monkeypatch):
    """No vault argument is consulted, so no vault is authorized either —
    which is what keeps `akb_help(topic="quickstart", vault=X)` from
    arming the injector."""
    import tempfile

    from app.config import settings
    settings.git_storage_path = tempfile.mkdtemp(prefix="akb-help-skill-test-")
    from mcp_server import server as server_mod

    async def _never(*a, **k):
        raise AssertionError("static topics must not access-check a vault")

    monkeypatch.setattr(server_mod, "check_vault_access", _never)

    handler = server_mod._HANDLERS["akb_help"]
    out = await handler(
        {"topic": "quickstart", "vault": "someone-elses"}, "u1", server_mod._MCPUser(),
    )
    assert "help" in out


@pytest.mark.asyncio
async def test_explicit_help_does_not_render_upstream_mirror_instructions(monkeypatch):
    import tempfile

    from app.config import settings
    settings.git_storage_path = tempfile.mkdtemp(prefix="akb-help-skill-test-")
    from app.services import vault_skill_service
    from mcp_server import server as server_mod

    async def allow(uid, vault, required_role="reader", **kw):
        return {"vault_id": "11111111-1111-1111-1111-111111111111"}

    async def mirror(vault, vault_id):
        return None

    async def never_read(*args, **kwargs):
        raise AssertionError("mirror help must not read upstream markdown")

    monkeypatch.setattr(server_mod, "check_vault_access", allow)
    monkeypatch.setattr(vault_skill_service, "fetch_for_authorized_reader", mirror)
    monkeypatch.setattr(server_mod.doc_service, "get", never_read)

    out = await server_mod._HANDLERS["akb_help"](
        {"topic": "vault-skill", "vault": "mirror-v"},
        "u1",
        server_mod._MCPUser(),
    )
    assert "read-only" in out["help"].lower() or "mirror" in out["help"].lower()
    assert "Source: vault owner" not in out["help"]
