"""Shared knowledge-graph vault-boundary primitives.

An edge belongs to exactly one vault. Application reads therefore scope both
the owning ``vault_id`` and the authority embedded in each endpoint URI. Keep
that predicate centralized so a new projection cannot accidentally count or
display a legacy cross-vault row.
"""

from __future__ import annotations

from app.services.uri_service import parse_uri


LINKABLE_RESOURCE_TYPES = ("doc", "table", "file")


def vault_uri_prefix(vault: str) -> str:
    """Return the unambiguous URI prefix for resources in ``vault``."""
    return f"akb://{vault}/"


def edge_scope_sql(
    *,
    alias: str = "",
    vault_param: int,
    prefix_param: int,
) -> str:
    """Return the SQL predicate for a reader-visible edge.

    Parameter numbers are explicit because callers compose the predicate into
    queries with different leading arguments. Values remain bound parameters;
    only trusted column aliases and integer placeholders are interpolated.
    """
    column = f"{alias}." if alias else ""
    return (
        f"{column}vault_id = ${vault_param} "
        f"AND starts_with({column}source_uri, ${prefix_param}) "
        f"AND starts_with({column}target_uri, ${prefix_param})"
    )


def uri_is_in_vault(uri: str, vault: str) -> bool:
    """Return whether ``uri`` is a linkable resource owned by ``vault``."""
    parsed = parse_uri(uri)
    return bool(
        parsed
        and parsed.vault == vault
        and parsed.kind in LINKABLE_RESOURCE_TYPES
        and parsed.identifier is not None
    )


def invalid_edge_predicate(*, edge_alias: str = "e", vault_alias: str = "v") -> str:
    """Return the aggregate audit predicate for an out-of-boundary edge."""
    return (
        f"NOT starts_with({edge_alias}.source_uri, "
        f"'akb://' || {vault_alias}.name || '/') "
        f"OR NOT starts_with({edge_alias}.target_uri, "
        f"'akb://' || {vault_alias}.name || '/')"
    )
