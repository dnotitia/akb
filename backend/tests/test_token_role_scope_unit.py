"""Unit tests for the M2 (PG-native) token-role scope intersection.

These cover the PURE logic that decides which vault group roles a scoped
PAT's ``akb_token_<tid>`` role becomes a member of — the heart of the
PG-native backstop — WITHOUT a live PostgreSQL. The DDL methods on
``RoleSync`` fetch the owner's accessible vaults from the catalog and feed
them to :func:`wanted_token_group_roles`; here we test that intersection
directly.

Invariant under test (escalation-impossible by construction): the token
role's membership is ``owner-ACL ∩ scope`` — a scope only ever SUBTRACTS,
so an out-of-scope vault is NEVER in the result, regardless of how strong
the owner's role on it is (owner/admin included).
"""

from __future__ import annotations

from app.models.vault_scope import VaultScope
from app.services.role_sync import (
    token_role_name,
    vault_group_role_name,
    wanted_token_group_roles,
)


GDN = VaultScope(prefixes=("gdn-",), extra_vaults=frozenset({"ops-shared"}))


class TestTokenRoleName:
    def test_dashes_become_underscores(self):
        tid = "11111111-2222-3333-4444-555555555555"
        assert token_role_name(tid) == "akb_token_11111111_2222_3333_4444_555555555555"

    def test_prefix_is_akb_token(self):
        assert token_role_name("abc").startswith("akb_token_")

    def test_accepts_uuid_object(self):
        import uuid

        u = uuid.UUID("11111111-2222-3333-4444-555555555555")
        assert token_role_name(u) == token_role_name(str(u))


class TestWantedTokenGroupRoles:
    def test_in_scope_prefix_included_with_its_role(self):
        accessible = [("v1", "gdn-state", "writer")]
        assert wanted_token_group_roles(accessible, GDN) == {
            vault_group_role_name("v1", "writer"),
        }

    def test_out_of_scope_excluded(self):
        accessible = [("v2", "product", "writer")]
        assert wanted_token_group_roles(accessible, GDN) == set()

    def test_extra_vaults_exact_match_included(self):
        accessible = [("v3", "ops-shared", "admin")]
        assert wanted_token_group_roles(accessible, GDN) == {
            vault_group_role_name("v3", "admin"),
        }

    def test_extra_vaults_is_exact_not_prefix(self):
        # "ops-shared" is an exact whitelist entry, not a prefix — a vault
        # that merely STARTS WITH it must NOT be admitted.
        accessible = [("v4", "ops-shared-secret", "writer")]
        assert wanted_token_group_roles(accessible, GDN) == set()

    def test_role_is_preserved_per_vault(self):
        accessible = [
            ("v1", "gdn-a", "reader"),
            ("v2", "gdn-b", "writer"),
            ("v3", "gdn-c", "admin"),
        ]
        assert wanted_token_group_roles(accessible, GDN) == {
            vault_group_role_name("v1", "reader"),
            vault_group_role_name("v2", "writer"),
            vault_group_role_name("v3", "admin"),
        }

    def test_mixed_scope_keeps_only_in_scope(self):
        accessible = [
            ("v1", "gdn-state", "admin"),     # in scope (prefix)
            ("v2", "product", "admin"),       # out of scope
            ("v3", "ops-shared", "writer"),   # in scope (extra)
            ("v4", "marketing", "writer"),    # out of scope
        ]
        assert wanted_token_group_roles(accessible, GDN) == {
            vault_group_role_name("v1", "admin"),
            vault_group_role_name("v3", "writer"),
        }

    def test_empty_accessible_is_empty(self):
        assert wanted_token_group_roles([], GDN) == set()

    # ── Adversarial: a scope NEVER widens, even for owner/admin ──

    def test_owner_admin_on_out_of_scope_vault_still_excluded(self):
        # The owner has the strongest role (admin) on an out-of-scope vault.
        # The token role must STILL NOT gain it — that is the whole point of
        # the backstop (a scoped gardener PAT cannot akb_sql-write a content
        # vault even though its user owns it).
        accessible = [("v2", "product", "admin")]
        assert wanted_token_group_roles(accessible, GDN) == set()

    def test_result_is_subset_of_unscoped(self):
        # For ANY scope, the scoped membership ⊆ the membership a scope that
        # permits every accessible vault would grant (intersection only
        # narrows — it can never add a role the owner doesn't already hold).
        accessible = [
            ("v1", "gdn-state", "admin"),
            ("v2", "product", "writer"),
            ("v3", "ops-shared", "reader"),
        ]
        catch_all = VaultScope(
            prefixes=tuple(name for (_id, name, _r) in accessible),
            extra_vaults=frozenset(),
        )
        scoped = wanted_token_group_roles(accessible, GDN)
        widest = wanted_token_group_roles(accessible, catch_all)
        assert scoped <= widest
        # GDN specifically excludes the out-of-scope product vault.
        assert vault_group_role_name("v2", "writer") not in scoped
