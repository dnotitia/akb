"""Unit test: create_vault seeds overview/vault-skill.md with type=skill."""
from app.services.document_service import (
    VAULT_SKILL_SEED_TEMPLATE,
    build_vault_skill_seed_request,
)


def test_seed_template_constant_exists():
    assert "Guide" in VAULT_SKILL_SEED_TEMPLATE
    assert "{vault}" in VAULT_SKILL_SEED_TEMPLATE  # substitutable
    assert "akb_put" not in VAULT_SKILL_SEED_TEMPLATE  # template is for owners to edit, not call-instructions


def test_canonical_seed_request_preserves_legacy_fields_and_body():
    vault = "parity-vault"
    request = build_vault_skill_seed_request(vault)

    assert request.model_dump(exclude_none=True) == {
        "vault": vault,
        "collection": "overview",
        "title": "parity-vault Guide",
        "type": "skill",
        "slug": "vault-skill",
        "tags": ["akb:skill"],
        "content": VAULT_SKILL_SEED_TEMPLATE.replace("{vault}", vault),
        "status": "draft",
        "related_to": [],
        "depends_on": [],
        "title_conflict_policy": "allow",
    }


def test_seed_template_secrets_placeholder_literal():
    """The seed template uses ${{secrets.X}} (double-brace), not ${secrets.X}."""
    assert "${{secrets.X}}" in VAULT_SKILL_SEED_TEMPLATE
    assert "${secrets.X}" not in VAULT_SKILL_SEED_TEMPLATE.replace("${{secrets.X}}", "")


def test_seed_template_includes_document_template_section():
    """The Document Template section must be in the seed body between Relation rules and Do not."""
    assert "## Document Template" in VAULT_SKILL_SEED_TEMPLATE
    # Section ordering check
    rel_idx = VAULT_SKILL_SEED_TEMPLATE.index("## Relation rules")
    tmpl_idx = VAULT_SKILL_SEED_TEMPLATE.index("## Document Template")
    do_not_idx = VAULT_SKILL_SEED_TEMPLATE.index("## Do not")
    assert rel_idx < tmpl_idx < do_not_idx
    # Skeleton content
    assert "## Purpose" in VAULT_SKILL_SEED_TEMPLATE
    assert "## Background" in VAULT_SKILL_SEED_TEMPLATE
    assert "## Decision / Result" in VAULT_SKILL_SEED_TEMPLATE
    assert "## Verification" in VAULT_SKILL_SEED_TEMPLATE
    assert "## Related" in VAULT_SKILL_SEED_TEMPLATE
    # Frontmatter example
    assert "---\ntitle:" in VAULT_SKILL_SEED_TEMPLATE
    assert "type: note|report|decision|spec|plan|session|reference" in VAULT_SKILL_SEED_TEMPLATE
