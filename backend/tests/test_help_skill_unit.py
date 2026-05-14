"""Unit tests for akb_help(topic='vault-skill', vault?) routing."""
import asyncio

from mcp_server.help import (
    VAULT_SKILL_TOPIC_BODY,
    VAULT_SKILL_PATH,
    render_vault_skill_response,
)


def test_topic_body_constant_exists():
    """The static topic body explains the convention without needing a vault."""
    assert "vault-skill" in VAULT_SKILL_TOPIC_BODY.lower()
    assert "akb_put" in VAULT_SKILL_TOPIC_BODY  # tells owner how to create one


def test_vault_skill_path_constant():
    """The doc path is fixed at overview/vault-skill.md."""
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
    """When the doc is missing, return notice + akb_put template + fallback rules."""
    async def fake_fetch(vault, doc_id):
        return None  # sentinel: doc not found

    out = asyncio.run(render_vault_skill_response(vault="empty-vault", fetch_fn=fake_fetch))
    assert "# Vault skill for empty-vault" in out
    assert "No `overview/vault-skill.md` found" in out
    assert "akb_put(" in out
    assert "akb_browse before writing" in out  # fallback bullet
    assert "${secrets.X}" in out  # secrets fallback


def test_render_without_vault_arg():
    """When no vault arg, returns just the static topic body."""
    out = asyncio.run(render_vault_skill_response(vault=None, fetch_fn=None))
    assert out == VAULT_SKILL_TOPIC_BODY
