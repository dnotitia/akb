"""Sole entrypoint for executing user-supplied SQL under per-user PG role.

`akb_sql` (and the REST `/api/v1/tables/{vault}/sql` endpoint) routes
through this class. Inside a transaction we `SET LOCAL ROLE` to the
caller's `akb_user_<uid>` role and let PostgreSQL enforce vault
isolation via its native ACL — no application-side identifier
filtering required.

`SET LOCAL` is transaction-scoped: it resets automatically on commit
or rollback, so the same pooled connection can serve different users
in subsequent transactions without cross-contamination. Compatible
with PgBouncer transaction-pool.

System admins (`users.is_admin = TRUE`) bypass `SET LOCAL ROLE`. Their
SQL runs under the connection's default role (the backend service
role) so they retain unrestricted access — matching existing trust
model.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

import asyncpg

from app.services.role_sync import user_role_name

logger = logging.getLogger("akb.user_sql")


# ── Result shaping ────────────────────────────────────────────


def _coerce_value(v: Any) -> Any:
    """Make `v` JSON-friendly for the MCP response envelope."""
    if isinstance(v, uuid.UUID):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (int, float, str, bool, type(None))):
        return v
    return str(v)


def _coerce_row(row: asyncpg.Record) -> dict:
    return {k: _coerce_value(v) for k, v in dict(row).items()}


# ── Errors ────────────────────────────────────────────────────


class PermissionDeniedError(Exception):
    """Raised when PG returns SQLSTATE 42501 for the user-supplied SQL.

    Holds the original PG message so callers can surface it verbatim.
    The fact that this error came from PG (and not from app validation)
    is itself meaningful: it means PG ACL did the enforcing, not the
    application sandbox.
    """

    def __init__(self, message: str, pg_sqlstate: str = "42501") -> None:
        super().__init__(message)
        self.pg_sqlstate = pg_sqlstate


class UniqueViolationError(Exception):
    """Raised when PG returns SQLSTATE 23505 for the user-supplied SQL.

    An INSERT/UPDATE that breaks a declared unique key (#215) surfaces
    here. Like `PermissionDeniedError`, we hold the PG message and
    sqlstate so the caller can pin a *stable* envelope (a dedicated
    code + `pg_sqlstate='23505'`) rather than letting 23505 fall
    through the generic SQL_ERROR catch-all. The generated/declared
    constraint name is embedded in the PG message verbatim.
    """

    def __init__(self, message: str, pg_sqlstate: str = "23505") -> None:
        super().__init__(message)
        self.pg_sqlstate = pg_sqlstate


# ── Executor ──────────────────────────────────────────────────


class UserSqlExecutor:
    """Run user-supplied SQL under the caller's PG role.

    The class holds a reference to the pool (same pool the rest of the
    backend uses; no separate pool). Per-call:
      1. Open transaction.
      2. SET LOCAL ROLE akb_user_<uid>  (skipped if `is_admin=True`).
      3. SET LOCAL search_path = public.
      4. Execute SQL.
      5. Commit (or rollback on exception).

    Step 2's role is reset by COMMIT/ROLLBACK, so reuse of the pooled
    connection across users is safe.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def execute(
        self,
        *,
        user_id: uuid.UUID | str,
        sql: str,
        is_admin: bool = False,
        vault_names: Optional[list[str]] = None,
    ) -> dict:
        """Run `sql` under the caller's PG role. Returns a result envelope
        compatible with the existing `akb_sql` contract.

        `vault_names` is included in the response envelope for
        downstream consumers but does NOT gate execution — PG ACL does.
        """
        is_select = sql.strip().upper().startswith(("SELECT", "WITH"))
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    if not is_admin:
                        role = user_role_name(user_id)
                        # asyncpg passes the role name as an identifier here;
                        # we built it ourselves from a UUID so the only chars
                        # are [a-z0-9_]. No injection risk.
                        await conn.execute(f'SET LOCAL ROLE "{role}"')
                    # Reset search_path defensively — a previous tx on this
                    # pooled connection might have left a different default
                    # (it shouldn't, but be explicit).
                    await conn.execute("SET LOCAL search_path = public")

                    if is_select:
                        rows = await conn.fetch(sql)
                        return {
                            "kind": "table_query",
                            "vaults": vault_names or [],
                            "columns": list(dict(rows[0]).keys()) if rows else [],
                            "items": [_coerce_row(r) for r in rows],
                            "total": len(rows),
                        }
                    result = await conn.execute(sql)
                    return {
                        "kind": "table_sql",
                        "vaults": vault_names or [],
                        "result": result,
                    }
        except asyncpg.exceptions.InsufficientPrivilegeError as e:
            # SQLSTATE 42501 — PG ACL denied. This is the *successful*
            # negative path: vault isolation is being enforced by PG.
            logger.info(
                "PG denied user_sql for user=%s: %s", user_id, e,
            )
            raise PermissionDeniedError(str(e), pg_sqlstate="42501") from e
        except asyncpg.exceptions.UniqueViolationError as e:
            # SQLSTATE 23505 — an INSERT/UPDATE broke a declared unique
            # key (#215). This is the *contracted* conflict path: surface
            # it under a dedicated stable code with pg_sqlstate attached
            # so callers can key off 23505 (and the constraint name in
            # the verbatim PG message), instead of the generic SQL_ERROR
            # catch-all that drops pg_sqlstate.
            logger.info(
                "PG unique violation in user_sql for user=%s: %s", user_id, e,
            )
            raise UniqueViolationError(str(e), pg_sqlstate="23505") from e
        except asyncpg.exceptions.UndefinedTableError:
            # 42P01 — table doesn't exist OR exists but role can't see it.
            # PG reports the same code for both ("relation X does not
            # exist"); we surface verbatim and let the caller's friendly-
            # hint logic offer fuzzy suggestions for tables in the
            # caller's allowed set.
            raise


# ── Singleton accessor ────────────────────────────────────────


_executor: Optional[UserSqlExecutor] = None


def get_user_sql_executor() -> UserSqlExecutor:
    """Return the process-global executor. Initialized once in
    `lifecycle.init_storage` after the pool is ready, matching the
    `RoleSync` singleton pattern."""
    if _executor is None:
        raise RuntimeError(
            "UserSqlExecutor not initialized — call set_user_sql_executor() "
            "during backend startup"
        )
    return _executor


def set_user_sql_executor(executor: UserSqlExecutor) -> None:
    global _executor
    _executor = executor
