"""Unit test: create_vault seeds overview/vault-skill.md with type=skill."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.document_service import DocumentService, VAULT_SKILL_SEED_TEMPLATE


def test_seed_template_constant_exists():
    assert "Vault Skill" in VAULT_SKILL_SEED_TEMPLATE
    assert "{vault}" in VAULT_SKILL_SEED_TEMPLATE  # substitutable
    assert "akb_put" not in VAULT_SKILL_SEED_TEMPLATE  # template is for owners to edit, not call-instructions


def test_seed_template_secrets_placeholder_literal():
    """The seed template uses ${{secrets.X}} (double-brace), not ${secrets.X}."""
    assert "${{secrets.X}}" in VAULT_SKILL_SEED_TEMPLATE
    assert "${secrets.X}" not in VAULT_SKILL_SEED_TEMPLATE.replace("${{secrets.X}}", "")


@pytest.mark.skip(reason="Covered by test_skill_e2e.sh; unit harness can't easily mock GitService.")
def test_seed_runs_after_template_apply():
    """create_vault writes overview/vault-skill.md after collections are seeded."""
    # NOTE: This is a thin behavioral test. The integration check happens in
    # the E2E suite (test_skill_e2e.sh). Here we just ensure the seed function
    # is called by inspecting the git commit log on a real but ephemeral vault
    # — easier to do this in E2E. Skip-mark this if the harness doesn't run it.
    pass
