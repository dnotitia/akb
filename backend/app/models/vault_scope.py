"""Per-PAT vault scope — the token-scoping value model.

A token MAY carry a vault scope: a set of vault-name ``prefixes`` plus an
explicit ``extra_vaults`` whitelist. A request's effective WRITE
permission is ``user-ACL ∩ vault_scope`` — an intersection, so a scope
only ever SUBTRACTS authority and is escalation-impossible by
construction.

A NULL ``tokens.vault_scope`` column means *unscoped* (the historical
full-ACL behaviour) and is represented as ``None`` — never as an empty
``VaultScope`` (which permits nothing). The enforcement layer
(``access_service.check_vault_access``) reads the request's scope from
the ``current_vault_scope`` ContextVar (set once per request when the
token is resolved) and treats ``None`` as "no restriction"; a concrete
``VaultScope`` gates mutating roles (writer/admin/owner) only — reads are
unrestricted (a scoped agent still READS broadly, it just can't WRITE
outside its scope).
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from dataclasses import dataclass

from app.exceptions import ValidationError

# A vault-name token: lowercase alphanumerics + hyphens, leading alnum.
# Used for both prefixes (e.g. "gdn-") and exact extra_vaults (e.g.
# "slack-ops") at mint-time well-formedness validation.
_SCOPE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class VaultScope:
    """An immutable (prefixes ∪ extra_vaults) membership gate."""

    prefixes: tuple[str, ...]
    extra_vaults: frozenset[str]

    def permits(self, vault_name: str) -> bool:
        """True iff ``vault_name`` is inside the scope (prefix match OR exact whitelist).

        An empty prefix is ignored (it would otherwise match everything);
        mint-time validation rejects empties, this is belt-and-suspenders.
        """
        if any(prefix and vault_name.startswith(prefix) for prefix in self.prefixes):
            return True
        return vault_name in self.extra_vaults

    def to_db_json(self) -> dict[str, list[str]]:
        """Canonical (sorted) JSONB shape for the ``tokens.vault_scope`` column."""
        return {
            "prefixes": sorted(self.prefixes),
            "extra_vaults": sorted(self.extra_vaults),
        }

    @classmethod
    def from_db_json(cls, raw: object) -> VaultScope | None:
        """Parse the JSONB column. ``None`` (NULL column) ⇒ ``None`` (unscoped).

        asyncpg may hand back a JSONB value as a parsed ``dict`` OR (legacy
        rows / some configs) a JSON string literal — both are accepted
        (mirrors ``table_registry_repo.parse_json_list``). Raises
        ``ValueError`` on a malformed shape so a corrupt column surfaces
        loudly rather than silently degrading to unscoped.
        """
        if raw is None:
            return None
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            raise ValueError(
                f"vault_scope must be a JSON object, got {type(raw).__name__}"
            )
        prefixes = raw.get("prefixes", [])
        extra = raw.get("extra_vaults", [])
        if not isinstance(prefixes, list) or not isinstance(extra, list):
            raise ValueError("vault_scope.prefixes and .extra_vaults must be arrays")
        return cls(
            prefixes=tuple(sorted(str(p) for p in prefixes)),
            extra_vaults=frozenset(str(v) for v in extra),
        )

    @classmethod
    def parse_input(cls, raw: object) -> VaultScope | None:
        """Validate + build a scope from caller mint input (stricter than the DB read).

        ``None`` ⇒ ``None`` (unscoped). Raises
        :class:`app.exceptions.ValidationError` (→ 400) on a malformed or
        empty scope so a caller cannot store a no-op or injection-shaped
        scope. Note: a scope is escalation-impossible regardless (effective
        = ACL ∩ scope), so this is input hygiene, not the security boundary.
        """
        if raw is None:
            return None
        try:
            scope = cls.from_db_json(raw)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        assert scope is not None  # raw is not None ⇒ from_db_json returns a scope
        if not scope.prefixes and not scope.extra_vaults:
            raise ValidationError(
                "vault_scope must declare at least one prefix or extra_vault"
            )
        for prefix in scope.prefixes:
            if not _SCOPE_NAME_RE.match(prefix):
                raise ValidationError(f"invalid vault_scope prefix: {prefix!r}")
        for vault in scope.extra_vaults:
            if not _SCOPE_NAME_RE.match(vault):
                raise ValidationError(f"invalid vault_scope extra_vault: {vault!r}")
        return scope


# Request-scoped carrier for the authenticated token's vault scope. Set once
# per request in ``auth_service.resolve_token`` (the single chokepoint BOTH the
# REST ``get_current_user`` dependency AND the MCP server's auth go through),
# read in ``access_service.check_vault_access``. Default ``None`` (unscoped) —
# tokenless internal/worker paths never set it.
current_vault_scope: ContextVar[VaultScope | None] = ContextVar(
    "current_vault_scope", default=None
)
