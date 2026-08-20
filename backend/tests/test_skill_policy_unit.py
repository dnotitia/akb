"""Guard matrix for the reserved overview namespace (two-way skill reservation).

Spec: docs/design/proposal/2026-08-19-vault-skill-system-collection/README.md
"""
import pytest

from app.exceptions import ForbiddenError
from app.services import skill_policy as sp


class TestReservedNamespace:
    def test_exact_overview_is_reserved(self):
        assert sp.is_reserved_collection("overview") is True

    def test_nested_overview_is_reserved(self):
        assert sp.is_reserved_collection("overview/sub") is True

    def test_prefix_lookalike_not_reserved(self):
        assert sp.is_reserved_collection("overview-notes") is False

    def test_root_and_other_collections_not_reserved(self):
        assert sp.is_reserved_collection("") is False
        assert sp.is_reserved_collection(None) is False
        assert sp.is_reserved_collection("notes") is False


class TestCheckPut:
    def test_blocks_any_type_into_overview(self):
        with pytest.raises(ForbiddenError):
            sp.check_put("overview", "note")

    def test_blocks_nested_overview(self):
        with pytest.raises(ForbiddenError):
            sp.check_put("overview/deep", "note")

    def test_blocks_skill_type_anywhere(self):
        with pytest.raises(ForbiddenError):
            sp.check_put("notes", "skill")
        with pytest.raises(ForbiddenError):
            sp.check_put("", "skill")

    def test_internal_bypass_allows_seed(self):
        sp.check_put("overview", "skill", internal=True)  # must not raise

    def test_normal_put_passes(self):
        sp.check_put("notes", "note")
        sp.check_put(None, "report")


class TestCheckUpdateType:
    def test_canonical_may_keep_skill(self):
        sp.check_update_type(sp.VAULT_SKILL_PATH, "skill")
        sp.check_update_type(sp.VAULT_SKILL_PATH, None)  # type untouched

    def test_canonical_retype_blocked(self):
        with pytest.raises(ForbiddenError):
            sp.check_update_type(sp.VAULT_SKILL_PATH, "note")

    def test_retype_to_skill_elsewhere_blocked(self):
        with pytest.raises(ForbiddenError):
            sp.check_update_type("notes/a.md", "skill")

    def test_normal_retype_passes(self):
        sp.check_update_type("notes/a.md", "report")
        sp.check_update_type("notes/a.md", None)

    def test_empty_string_type_is_ignored(self):
        sp.check_update_type(sp.VAULT_SKILL_PATH, "")
        sp.check_update_type("notes/a.md", "")


class TestCheckUpdateAuthority:
    def test_canonical_write_requires_owner_authorized_internal_lane(self):
        with pytest.raises(ForbiddenError, match="vault owner"):
            sp.check_update(sp.VAULT_SKILL_PATH, None)
        sp.check_update(sp.VAULT_SKILL_PATH, None, internal=True)

    def test_ordinary_document_write_remains_available(self):
        sp.check_update("notes/a.md", None)


class TestCheckMove:
    def test_move_out_of_overview_blocked(self):
        with pytest.raises(ForbiddenError):
            sp.check_move(sp.VAULT_SKILL_PATH, "notes/vault-skill.md")

    def test_move_into_overview_blocked(self):
        with pytest.raises(ForbiddenError):
            sp.check_move("notes/a.md", "overview/a.md")

    def test_internal_bypass_for_migration(self):
        sp.check_move(sp.VAULT_SKILL_PATH, "notes/x.md", internal=True)

    def test_normal_move_passes(self):
        sp.check_move("notes/a.md", "reports/a.md")


class TestCheckDelete:
    def test_reserved_paths_not_deletable(self):
        with pytest.raises(ForbiddenError):
            sp.check_delete(sp.VAULT_SKILL_PATH)
        with pytest.raises(ForbiddenError):
            sp.check_delete("overview/anything.md")

    def test_normal_delete_passes(self):
        sp.check_delete("notes/a.md")


class TestCheckCollectionDelete:
    def test_overview_blocked_but_legacy_subtree_can_be_cleaned(self):
        with pytest.raises(ForbiddenError):
            sp.check_collection_delete("overview")
        sp.check_collection_delete("overview/sub")

    def test_other_collections_pass(self):
        sp.check_collection_delete("notes")
        sp.check_collection_delete("overview-notes")


class TestCheckNonDocResource:
    def test_files_tables_blocked_under_overview(self):
        with pytest.raises(ForbiddenError):
            sp.check_resource_collection("overview")
        with pytest.raises(ForbiddenError):
            sp.check_resource_collection("overview/assets")

    def test_elsewhere_passes(self):
        sp.check_resource_collection("assets")
        sp.check_resource_collection(None)
