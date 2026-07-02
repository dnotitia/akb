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
from typing import Any

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

# Request-scoped carrier for the authenticated PAT's id, set alongside
# ``current_vault_scope`` in ``auth_service.resolve_token``. The PG-native
# ``akb_sql`` executor (``user_sql_executor.execute``) reads it to
# ``SET LOCAL ROLE akb_token_<tid>`` when a scope is present — the narrow role
# whose membership is the owner-ACL ∩ scope (surface 2 of the backstop). The
# executor only consults it when ``current_vault_scope`` is also set, so a
# JWT / unscoped-PAT / worker path (``None``) runs under ``akb_user_<uid>``
# (or the admin bypass) exactly as before.
current_token_id: ContextVar[str | None] = ContextVar(
    "current_token_id", default=None
)

# Request-scoped token metadata, set by ``auth_service.resolve_token`` for
# rows from the tokens table. JWT / OAuth / internal paths leave these as
# None; PAT and service-key paths set both so entrypoints can enforce coarse
# read/write scopes and AKB-038 can gate trusted claim injection on
# ``current_key_class == "service"``.
current_key_class: ContextVar[str | None] = ContextVar(
    "current_key_class", default=None
)
current_token_scopes: ContextVar[frozenset[str] | None] = ContextVar(
    "current_token_scopes", default=None
)


@dataclass(frozen=True)
class RequestJwtClaims:
    """Validated end-user claims trusted for transaction-local RLS GUCs."""

    sub: str
    org_id: str
    role: str

    def to_db_json(self) -> dict[str, Any]:
        return {
            "sub": self.sub,
            "app_metadata": {
                "org_id": self.org_id,
                "role": self.role,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_db_json(), separators=(",", ":"), sort_keys=True)


_CLAIMS_HEADER_MAX_BYTES = 8192


def parse_request_jwt_claims_header(raw: str) -> RequestJwtClaims:
    """Parse the AKB BaaS claim-injection header.

    The trusted contract is intentionally narrow for T3/T4: callers may only
    inject the invariant-A shape ``{sub, app_metadata:{org_id, role}}`` and
    downstream code receives a canonicalized JSON object with those fields.
    """
    if len(raw.encode("utf-8")) > _CLAIMS_HEADER_MAX_BYTES:
        raise ValueError("X-Akb-Claims is too large")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("X-Akb-Claims must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("X-Akb-Claims must be a JSON object")

    sub = _required_claim_str(parsed.get("sub"), "sub")
    app_metadata = parsed.get("app_metadata")
    if not isinstance(app_metadata, dict):
        raise ValueError("X-Akb-Claims.app_metadata must be a JSON object")
    org_id = _required_claim_str(app_metadata.get("org_id"), "app_metadata.org_id")
    role = _required_claim_str(app_metadata.get("role"), "app_metadata.role")
    return RequestJwtClaims(sub=sub, org_id=org_id, role=role)


def _required_claim_str(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"X-Akb-Claims.{path} must be a non-empty string")
    return value


current_request_jwt_claims: ContextVar[RequestJwtClaims | None] = ContextVar(
    "current_request_jwt_claims", default=None
)
