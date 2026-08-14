"""Shared app-owned table identity, fingerprint, and mutation guards.

Legacy adoption and structured rollout must agree on what a table baseline is
and on who is allowed to mutate an app-owned table.  This module keeps that
definition independent from either workflow.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
import uuid

import asyncpg

from app.exceptions import ConflictError, ValidationError
from app.util.text import to_nfc_any

_TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TableOwnershipContext:
    """The only context that may mutate an adopted table."""

    installation_id: uuid.UUID
    app_id: uuid.UUID


def canonical_json(value: Any) -> bytes:
    """Return the repository-wide NFC + sorted-key JSON representation."""

    return json.dumps(
        to_nfc_any(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_table_descriptor(row: Any) -> dict[str, Any]:
    """Project only the schema fields that belong to a table baseline."""

    def _list(value: Any) -> list[Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
        return list(value) if isinstance(value, list) else []

    return {
        "name": str(row["name"]),
        "columns": _list(row.get("columns") if hasattr(row, "get") else row["columns"]),
        "unique_keys": _list(
            row.get("unique_keys") if hasattr(row, "get") else row["unique_keys"]
        ),
        "indexes": _list(row.get("indexes") if hasattr(row, "get") else row["indexes"]),
    }


def canonical_table_fingerprint(rows: Iterable[Any]) -> str:
    """Hash a sorted collection of table descriptors.

    Only rows supplied by the caller are included.  Callers therefore control
    the allowlist query and cannot accidentally broaden an adoption baseline
    by discovering unrelated tables.
    """

    descriptors = [canonical_table_descriptor(row) for row in rows]
    descriptors.sort(key=lambda item: item["name"])
    return hashlib.sha256(canonical_json(descriptors)).hexdigest()


def normalize_table_allowlist(value: Iterable[str]) -> list[str]:
    """Normalize and validate an explicit table allowlist."""

    if not isinstance(value, list):
        raise ValidationError("table allowlist must be an array")
    values: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValidationError("table allowlist must contain strings")
        name = item.strip()
        if not _TABLE_NAME_RE.fullmatch(name):
            raise ValidationError("table allowlist contains an invalid table name")
        values.add(name)
    if not values:
        raise ValidationError("table allowlist must not be empty")
    return sorted(values)


def normalize_fingerprint(value: str, *, field: str = "schema fingerprint") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.strip().lower()):
        raise ValidationError(f"{field} must be a SHA-256 checksum")
    return value.strip().lower()


async def fetch_allowlisted_tables(
    conn: Any,
    vault_id: uuid.UUID,
    table_allowlist: list[str],
    *,
    lock: bool = False,
) -> list[dict[str, Any]]:
    """Read only the table names explicitly present in ``table_allowlist``."""

    rows = await conn.fetch(
        """
        SELECT id, vault_id, name, description, columns, unique_keys, indexes
          FROM vault_tables
         WHERE vault_id = $1 AND name = ANY($2::text[])
         ORDER BY name
        """ + (" FOR SHARE" if lock else ""),
        vault_id,
        table_allowlist,
    )
    return [dict(row) for row in rows]


async def table_ownership(
    conn: Any,
    vault_id: uuid.UUID,
    table_name: str,
) -> dict[str, Any] | None:
    # init.sql deliberately contains only the legacy table surface.  Probe
    # before the ownership query so a real transaction is never aborted by an
    # ``UndefinedTableError`` on a pre-registry database.
    fetchval = getattr(conn, "fetchval", None)
    if fetchval is not None and await fetchval("SELECT to_regclass('public.app_owned_resources')") is None:
        return None
    try:
        row = await conn.fetchrow(
            """
            SELECT resource.installation_id, installation.app_id, resource.status
              FROM app_owned_resources AS resource
              JOIN vault_app_installations AS installation
                ON installation.id = resource.installation_id
             WHERE resource.vault_id = $1
               AND resource.resource_kind = 'table'
               AND resource.resource_key = $2
            """,
            vault_id,
            table_name,
        )
    except asyncpg.UndefinedTableError:
        # A concurrent schema bootstrap may still race the probe.  Preserve
        # the legacy mutation path if the registry disappears between reads.
        return None
    if not row:
        return None
    result = dict(row)
    # The SQL join always supplies these fields.  Keep the shape check so an
    # unrelated registry row can never be treated as ownership.
    if result.get("installation_id") is None or result.get("status") not in {"owned", "retained"}:
        return None
    return result


async def ensure_table_mutation_allowed(
    conn: Any,
    vault_id: uuid.UUID,
    table_name: str,
    *,
    context: TableOwnershipContext | None = None,
) -> None:
    """Reject generic schema mutation of owned or retained app tables.

    A rollout worker may pass the exact app + installation execution context,
    but even that context is valid only for an ``owned`` row in the same Vault.
    """

    ownership = await table_ownership(conn, vault_id, table_name)
    if ownership is None:
        return
    if (
        context is not None
        and ownership["status"] == "owned"
        and ownership["installation_id"] == context.installation_id
        and ownership["app_id"] == context.app_id
    ):
        return
    raise ConflictError("Table is managed by an app installation")


def table_mutation_lock_key(vault_id: uuid.UUID, table_name: str) -> str:
    """Return the transaction lock key shared by adoption and table writes."""

    return f"app-table:{vault_id}:{table_name}"


def app_vault_lock_key(app_id: uuid.UUID, vault_id: uuid.UUID) -> str:
    """Return the lifecycle lock key shared by installation/adoption paths."""

    return f"app-installation:{app_id}:{vault_id}"


async def lock_app_vault_pair(conn: Any, app_id: uuid.UUID, vault_id: uuid.UUID) -> None:
    """Serialize installation lifecycle and legacy adoption for one pair."""

    fetchval = getattr(conn, "fetchval", None)
    if fetchval is None:
        return
    await fetchval(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        app_vault_lock_key(app_id, vault_id),
    )


async def lock_table_mutation(conn: Any, vault_id: uuid.UUID, table_name: str) -> None:
    """Serialize adoption with generic table DDL for one logical table."""

    # A few DB-free service tests pass deliberately minimal connection fakes;
    # real asyncpg connections always expose fetchval.
    fetchval = getattr(conn, "fetchval", None)
    if fetchval is None:
        return
    await fetchval(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        table_mutation_lock_key(vault_id, table_name),
    )


def table_name_is_valid(value: str) -> bool:
    return isinstance(value, str) and _TABLE_NAME_RE.fullmatch(value) is not None
