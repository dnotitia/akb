"""Unit coverage for the per-PAT VaultScope value model.

DB-free by construction: VaultScope is a pure value type — the
(prefixes ∪ extra_vaults) membership test that the authorization layer
intersects against a token's effective WRITE permission. A NULL token
scope means *unscoped* (the historical full-ACL behaviour) and is
represented as ``None``, never an empty VaultScope (which permits
nothing). These tests pin ``permits()``, the JSONB round-trip, the
mint-input validation (``parse_input``), the request-scoped ContextVar,
and the adversarial "a scope only ever SUBTRACTS — it never permits a
vault outside its declared sets" invariant.
"""

from __future__ import annotations

import pytest

from app.exceptions import ValidationError
from app.models.vault_scope import VaultScope, current_vault_scope


class TestPermits:
    def test_prefix_permits_matching_vaults(self) -> None:
        scope = VaultScope(prefixes=("gdn-",), extra_vaults=frozenset())
        assert scope.permits("gdn-state")
        assert scope.permits("gdn-lint")
        assert scope.permits("gdn-")  # exact-prefix boundary

    def test_prefix_denies_non_matching(self) -> None:
        scope = VaultScope(prefixes=("gdn-",), extra_vaults=frozenset())
        assert not scope.permits("product")
        assert not scope.permits("collector-dev")
        assert not scope.permits("gd")  # shorter than the prefix
        assert not scope.permits("xgdn-state")  # prefix not at the start

    def test_extra_vaults_permit_exact_non_gdn(self) -> None:
        scope = VaultScope(prefixes=("gdn-",), extra_vaults=frozenset({"slack-ops"}))
        assert scope.permits("slack-ops")  # whitelisted non-gdn
        assert scope.permits("gdn-state")  # still permits the prefix
        assert not scope.permits("slack-ops-2")  # whitelist is exact, not a prefix

    def test_empty_scope_permits_nothing(self) -> None:
        scope = VaultScope(prefixes=(), extra_vaults=frozenset())
        assert not scope.permits("gdn-state")
        assert not scope.permits("anything")

    def test_multi_prefix(self) -> None:
        scope = VaultScope(prefixes=("gdn-", "lint-"), extra_vaults=frozenset())
        assert scope.permits("gdn-state")
        assert scope.permits("lint-x")
        assert not scope.permits("other")


class TestJsonRoundTrip:
    def test_to_db_json_shape(self) -> None:
        scope = VaultScope(prefixes=("gdn-",), extra_vaults=frozenset({"slack-ops"}))
        assert scope.to_db_json() == {"prefixes": ["gdn-"], "extra_vaults": ["slack-ops"]}

    def test_from_db_json_round_trip(self) -> None:
        scope = VaultScope.from_db_json(
            {"prefixes": ["gdn-"], "extra_vaults": ["slack-ops", "a-vault"]}
        )
        assert scope is not None
        assert scope.permits("gdn-x")
        assert scope.permits("slack-ops")
        assert scope.permits("a-vault")
        assert not scope.permits("nope")

    def test_from_db_json_none_is_none(self) -> None:
        # NULL column ⇒ None (unscoped), NOT an empty (deny-all) scope.
        assert VaultScope.from_db_json(None) is None

    def test_from_db_json_accepts_json_string(self) -> None:
        # asyncpg may return a JSONB column as a JSON string literal (legacy /
        # some configs); from_db_json must json.loads it, not choke.
        scope = VaultScope.from_db_json('{"prefixes": ["gdn-"], "extra_vaults": []}')
        assert scope is not None
        assert scope.permits("gdn-state")
        assert not scope.permits("product")

    def test_from_db_json_canonicalizes_order(self) -> None:
        # Deterministic serialize (sorted) so the stored column is stable.
        scope = VaultScope.from_db_json(
            {"prefixes": ["b-", "a-"], "extra_vaults": ["z", "a"]}
        )
        assert scope is not None
        assert scope.to_db_json() == {"prefixes": ["a-", "b-"], "extra_vaults": ["a", "z"]}

    def test_from_db_json_rejects_non_object(self) -> None:
        with pytest.raises(ValueError):
            VaultScope.from_db_json(["gdn-"])  # arrays / scalars are malformed

    def test_from_db_json_rejects_non_array_fields(self) -> None:
        with pytest.raises(ValueError):
            VaultScope.from_db_json({"prefixes": "gdn-", "extra_vaults": []})


class TestParseInput:
    """Mint-time validation: stricter than the trusted DB read."""

    def test_none_is_none(self) -> None:
        assert VaultScope.parse_input(None) is None

    def test_valid_scope_builds(self) -> None:
        scope = VaultScope.parse_input({"prefixes": ["gdn-"], "extra_vaults": ["slack-ops"]})
        assert scope is not None
        assert scope.permits("gdn-state")
        assert scope.permits("slack-ops")

    def test_empty_scope_rejected(self) -> None:
        # A scope that declares nothing is a no-op — reject it at mint.
        with pytest.raises(ValidationError):
            VaultScope.parse_input({"prefixes": [], "extra_vaults": []})

    def test_malformed_shape_wrapped_as_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            VaultScope.parse_input(["gdn-"])

    @pytest.mark.parametrize("bad", ["GDN-", "gdn_state", "gdn state", "-gdn", "", "x/y"])
    def test_bad_prefix_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            VaultScope.parse_input({"prefixes": [bad], "extra_vaults": []})

    @pytest.mark.parametrize("bad", ["Slack", "a_b", "a/b", "../x"])
    def test_bad_extra_vault_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            VaultScope.parse_input({"prefixes": [], "extra_vaults": [bad]})


class TestContextVar:
    def test_default_is_none(self) -> None:
        # A request that never set a scope (tokenless / JWT / worker) ⇒ unscoped.
        assert current_vault_scope.get() is None

    def test_set_and_get(self) -> None:
        scope = VaultScope(prefixes=("gdn-",), extra_vaults=frozenset())
        token = current_vault_scope.set(scope)
        try:
            assert current_vault_scope.get() is scope
        finally:
            current_vault_scope.reset(token)
        assert current_vault_scope.get() is None


class TestAdversarialNeverWiden:
    """A scope can only ever SUBTRACT — it never permits beyond its sets."""

    @pytest.mark.parametrize("vault", ["product", "collector-dev", "users", "tokens", ""])
    def test_gdn_scope_denies_arbitrary(self, vault: str) -> None:
        scope = VaultScope(prefixes=("gdn-",), extra_vaults=frozenset())
        assert not scope.permits(vault)
