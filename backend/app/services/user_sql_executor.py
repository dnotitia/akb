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

Human system admins (`users.is_admin = TRUE`) bypass `SET LOCAL ROLE`. Their
SQL runs under the connection's default role (the backend service
role) so they retain unrestricted access — matching existing trust
model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional

import asyncpg

from app.config import settings
from app.exceptions import NotFoundError, WriteBusyError
from app.models.vault_scope import (
    current_request_jwt_claims,
    current_token_id,
    current_token_uuid,
    current_vault_scope,
)
from app.repositories import vault_write_policy_repo
from app.repositories.events_repo import EventBuffer
from app.util.errors import VAULT_ARCHIVED, VAULT_WRITE_MANAGED, err
from app.services.role_sync import token_role_name, user_role_name
from app.services.vault_write_admission import (
    VaultReplacedError,
    is_admitted,
    retry_if_vault_replaced,
    vault_write_admission,
)

logger = logging.getLogger("akb.user_sql")


async def human_admin_sql_bypass(
    conn, user_id: uuid.UUID | str, is_admin: bool, *, lock: bool = True,
) -> bool:
    """Machine administration never delegates the backend database role.

    Read and lock the live account in the SQL transaction. A service PAT's
    administrative API status does not permit rewriting identity authority via
    SQL. Scoped token roles continue to take precedence for humans too.
    Without ``lock`` the account is only read, for planning a statement.
    """
    if not is_admin:
        return False
    return bool(await conn.fetchval(
        _HUMAN_ADMIN_FOR_SHARE if lock else _HUMAN_ADMIN, uuid.UUID(str(user_id)),
    ))


_HUMAN_ADMIN = """SELECT account_kind='human' AND is_admin AND account_status='active'
        FROM users WHERE id=$1"""
_HUMAN_ADMIN_FOR_SHARE = _HUMAN_ADMIN + " FOR SHARE"


_DECLARED_VAULTS = "SELECT id, name, status FROM vaults WHERE name = ANY($1::text[]) ORDER BY id"
_DECLARED_VAULTS_FOR_SHARE = _DECLARED_VAULTS + " FOR SHARE"


async def _check_vault_write_policy(
    conn: asyncpg.Connection, vault_names: list[str], user_id: uuid.UUID | str, *,
    admin_bypass: bool, overrides: EventBuffer | None, lock: bool = True,
    admitted: frozenset[uuid.UUID] | None = None,
) -> dict | None:
    """Return a policy error, or collect the admin overrides into ``overrides``
    (``None`` evaluates the policy without recording them).

    With ``lock``, hold each declared Vault against archive, mark and grant
    changes until the statement transaction finishes. The caller inserts the
    override events after the statement: an event insert holds the Vault's
    event lock until commit (migration 132), which must not be held while
    the statement runs or waits for rows.

    ``admitted`` is the set of Vaults the transaction was admitted for by id,
    resolved from the names before it took its connection. A declared name
    that now names another Vault raises ``VaultReplacedError``.
    """
    vaults = await conn.fetch(
        _DECLARED_VAULTS_FOR_SHARE if lock else _DECLARED_VAULTS, vault_names,
    )
    missing = set(vault_names) - {v["name"] for v in vaults}
    if missing:
        raise NotFoundError("Vault", min(missing))
    if admitted is not None and any(v["id"] not in admitted for v in vaults):
        raise VaultReplacedError
    archived = sorted(v["name"] for v in vaults if v["status"] == "archived")
    if archived:
        return err(
            f"Vault '{', '.join(archived)}' is archived (read-only); writes via "
            "akb_sql are not allowed. Unarchive the vault first.", code=VAULT_ARCHIVED,
        )

    # Read policy *after* locking. A grant may have changed while we waited
    # for its writer, which serializes on the parent Vault row.
    managed = await vault_write_policy_repo.sql_restricted_vaults(
        conn, [v["id"] for v in vaults], current_token_uuid(),
    )
    for policy in managed:
        if not admin_bypass:
            return err(
                f"Vault '{policy['name']}' is write-managed by "
                f"{policy['managed_by']}; writes require a granted service token",
                code=VAULT_WRITE_MANAGED,
            )
        if overrides is None:
            continue
        overrides.add(
            "vault.write_policy_admin_bypass", vault_id=policy["id"], actor_id=str(user_id),
            payload={"managed_by": policy["managed_by"], "required_role": "writer", "vault": policy["name"]},
        )
    return None


# ── Result shaping ────────────────────────────────────────────


def _coerce_value(v: Any) -> Any:
    """Make `v` JSON-friendly for the akb_sql response envelope.

    Non-finite floats (NaN/±Inf) are left as-is here — they are normalised to
    `null` only on the REST `tables.execute_sql` route, which serialises the
    envelope with `to_json(..., inf_nan_mode="null")` (the table viewer +
    snapshot consumer that a bare `NaN` token broke). The MCP `server.call_tool`
    path keeps its pre-hardening `json.dumps(default=str)` for byte-identical
    wire compatibility, so a non-finite float there still renders as a bare
    token exactly as before — a pre-existing edge, not widened by this change.
    """
    if isinstance(v, uuid.UUID):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (int, float, str, bool, type(None))):
        return v
    return str(v)


def _coerce_row(row: asyncpg.Record) -> dict:
    # asyncpg.Record supports .items() directly — no throwaway dict(row) copy.
    return {k: _coerce_value(v) for k, v in row.items()}


async def _coerce_rows_yielding(rows: list) -> list[dict]:
    """Coerce a full result set to JSON dicts WITHOUT blocking the event loop.

    `[_coerce_row(r) for r in rows]` is pure-Python CPU with no ``await``; on a
    million-row result it holds the single event loop for seconds and starves
    the liveness probe (→ 503). We chunk it and ``await asyncio.sleep(0)``
    between chunks so the loop can service other tasks (health probes, other
    requests) between batches. The result is NOT truncated — only the CPU work
    is interleaved.
    """
    batch = max(1, settings.akb_sql_coerce_batch)
    if len(rows) <= batch:
        return [_coerce_row(r) for r in rows]
    items: list[dict] = []
    for start in range(0, len(rows), batch):
        items.extend(_coerce_row(r) for r in rows[start:start + batch])
        await asyncio.sleep(0)  # yield to the loop between batches
    return items


def _affected_rows_from_command_tag(tag: str) -> int | None:
    """Return the affected-row count from asyncpg command tags.

    PostgreSQL emits tags such as ``INSERT 0 3``, ``UPDATE 5``, and
    ``DELETE 2``. Other command families can reach this executor through the
    raw SQL surface over time, so return ``None`` rather than guessing when the
    tag is not a DML row-count shape.
    """
    parts = tag.split()
    if not parts or parts[0] not in {"INSERT", "UPDATE", "DELETE"}:
        return None
    for token in reversed(parts[1:]):
        if token.isdigit():
            return int(token)
    return None


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


async def _run_statement(conn, sql: str, params: list, fetch: bool, vault_names: list[str]) -> dict:
    if fetch:
        rows = await conn.fetch(sql, *params)
        items = await _coerce_rows_yielding(rows)
        return {
            "kind": "table_query",
            "vaults": vault_names,
            # dict(...) collapses duplicate column labels the
            # same way each coerced `items` row does, so columns
            # and row keys stay consistent for `SELECT id, id`
            # or cross-vault joins that share a column name.
            "columns": list(dict(rows[0]).keys()) if rows else [],
            "items": items,
            "total": len(rows),
        }
    result = await conn.execute(sql, *params)
    out = {
        "kind": "table_sql",
        "vaults": vault_names,
        "result": result,
    }
    affected_rows = _affected_rows_from_command_tag(result)
    if affected_rows is not None:
        out["affected_rows"] = affected_rows
    return out


class UserSqlExecutor:
    """Run user-supplied SQL under the caller's PG role.

    The class holds a reference to the pool (same pool the rest of the
    backend uses; no separate pool). Per transaction:
      1. Open transaction.
      2. SET LOCAL ROLE:
           - scoped PAT     → akb_token_<tid>  (owner-ACL ∩ scope; even if admin)
           - else non-human-admin → akb_user_<uid>
           - else unscoped human admin → connection default (full)
      3. SET LOCAL search_path = public.
      4. Execute SQL.
      5. Commit (or rollback on exception).
    A statement runs first in a read-only transaction and, only when
    PostgreSQL refuses it there as a write, again in an admitted one (see
    `execute`).

    Step 2's role is reset by COMMIT/ROLLBACK, so reuse of the pooled
    connection across users is safe. The transaction-local ``akb.actor_id``
    GUC carries the authenticated actor to statement-level dynamic-table
    triggers without exposing the system events table to user SQL roles.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @retry_if_vault_replaced
    async def execute(
        self,
        *,
        user_id: uuid.UUID | str,
        actor_id: str,
        sql: str,
        params: list[Any] | None = None,
        fetch: bool | None = None,
        is_admin: bool = False,
        vault_names: Optional[list[str]] = None,
        read_only: bool = False,
        enforce_vault_write_policy: bool = False,
        vault_ids: Optional[list[uuid.UUID]] = None,
    ) -> dict:
        """Run `sql` under the caller's PG role. Returns a result envelope
        compatible with the existing `akb_sql` contract.

        `vault_names` is included in the response envelope for
        downstream consumers. Raw SQL also checks declared Vault write policies;
        PostgreSQL roles control access to the actual tables.

        With `read_only` the statement runs in a READ ONLY transaction, which
        PostgreSQL enforces, without the Vault write admission. Otherwise it is
        a write and runs exactly once: it is admitted, before it takes the
        connection it runs on, for the declared Vaults. `vault_names` declares
        the Vaults of raw SQL, and its plan, read with ``EXPLAIN`` without
        running it, must modify dynamic tables of declared Vaults only: one
        that modifies another Vault's is refused with
        `UndeclaredVaultWriteError` before its admission, administrators'
        statements included, and does not run. `vault_ids` is for a statement
        the server compiled for one table, whose caller names the Vaults it
        writes; its plan is not read. Before it commits, the transaction checks
        the row changes it queued for `table.rows_changed` events; a change of
        a Vault it was not admitted for, as a function the plan does not show
        can make, rolls it back with `UndeclaredVaultWriteError`.
        """
        should_fetch = (
            sql.strip().upper().startswith(("SELECT", "WITH"))
            if fetch is None else fetch
        )
        statement = _Statement(
            user_id=user_id, actor_id=actor_id, sql=sql, params=params or [],
            fetch=should_fetch, is_admin=is_admin, vault_names=vault_names or [],
            check_policy=enforce_vault_write_policy,
        )
        try:
            if read_only:
                return await self._run(statement, read_only=True, admitted=None)
            admitted = await self._declared(statement, vault_ids)
            if vault_ids is None:
                # A compiled statement's caller names the Vaults it writes.
                # Raw SQL may write only the Vaults it declares.
                undeclared = await self._planned(statement) - admitted
                if undeclared:
                    raise UndeclaredVaultWriteError(await self._vault_names(undeclared), ran=False)
            async with vault_write_admission(*admitted):
                return await self._run(statement, read_only=False, admitted=admitted)
        except asyncpg.exceptions.ReadOnlySQLTransactionError as e:
            raise PermissionDeniedError(str(e), pg_sqlstate="25006") from e
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
        except Exception as exc:
            from app.db.transient import transient_error

            mapped = transient_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    async def _planned(self, statement: _Statement) -> frozenset[uuid.UUID]:
        """The Vaults whose dynamic tables the statement's plan modifies,
        which ``execute`` requires to be declared.

        ``EXPLAIN`` plans the statement without running it, under the
        statement's role, in a read-only transaction on a pool connection
        taken before the statement's admission. Planning takes the locks the
        statement takes on its tables, so at most `akb_sql_plan_concurrency`
        plans are read at once in this process, and at most one per declared
        Vault (`_PlanSlots`). Once a plan has its slot, reading it, from the
        wait for a connection to the end of ``EXPLAIN``, may take
        `akb_sql_plan_timeout_ms` in all and each wait for a lock
        `akb_sql_plan_lock_timeout_ms`; past either the write fails with a
        retryable `write_busy` and gives its slot back, so a plan that waits
        for one lock after another cannot keep the slots from other Vaults'
        statements. A statement that cannot be planned is admitted for the
        Vaults it declares, and reports its own error when it runs. Foreign
        keys of a dynamic table only reference tables of its own Vault, so a
        cascade stays in a Vault the plan names.
        """
        async with _plan_slot(statement.vault_names):
            loop = asyncio.get_running_loop()
            started = loop.time()
            deadline = started + max(1, settings.akb_sql_plan_timeout_ms) / 1000
            try:
                return await self._read_plan(statement, deadline)
            except (
                TimeoutError,
                asyncpg.exceptions.LockNotAvailableError,
                asyncpg.exceptions.QueryCanceledError,
            ):
                raise WriteBusyError(
                    ", ".join(statement.vault_names) or "akb_sql", loop.time() - started,
                    retry_after_secs=1,
                ) from None

    async def _read_plan(self, statement: _Statement, deadline: float) -> frozenset[uuid.UUID]:
        """Read the plan of ``_planned`` by ``deadline``, in the event loop's
        clock: ``TimeoutError`` when no connection is free in time,
        ``LockNotAvailableError`` past a lock's timeout, and
        ``QueryCanceledError`` past the deadline."""
        loop = asyncio.get_running_loop()
        async with AsyncExitStack() as stack:
            async with asyncio.timeout_at(deadline):
                conn = await stack.enter_async_context(self.pool.acquire())
            async with conn.transaction():
                left_ms = int((deadline - loop.time()) * 1000)
                if left_ms < 1:
                    raise TimeoutError
                await conn.execute(
                    "SELECT set_config('lock_timeout', $1, true), "
                    "set_config('statement_timeout', $2, true)",
                    f"{max(1, settings.akb_sql_plan_lock_timeout_ms)}ms", f"{left_ms}ms",
                )
                try:
                    async with conn.transaction():
                        admin_bypass = await _admin_bypass(conn, statement, lock=False)
                        await _enter_statement_role(conn, statement, admin_bypass)
                        await conn.execute("SET TRANSACTION READ ONLY")
                        plan = await conn.fetchval(
                            "EXPLAIN (VERBOSE, FORMAT JSON) " + statement.sql, *statement.params,
                        )
                except (asyncpg.exceptions.LockNotAvailableError, asyncpg.exceptions.QueryCanceledError):
                    raise
                except asyncpg.PostgresError:
                    return frozenset()
                relations = _modified_relations(
                    json.loads(plan) if isinstance(plan, str) else plan,
                )
                if not relations:
                    return frozenset()
                # The trigger catalog is read under the backend role.
                await conn.execute("SET LOCAL ROLE NONE")
                schemas, tables = zip(*sorted(relations))
                rows = await conn.fetch(_VAULTS_OF_TABLES, list(schemas), list(tables))
        return frozenset(uuid.UUID(row["vault_id"]) for row in rows if row["vault_id"])

    async def _run(
        self, statement: _Statement, *, read_only: bool, admitted: frozenset[uuid.UUID] | None,
    ) -> dict:
        should_fetch = statement.fetch
        overrides = EventBuffer()
        outcome: dict = {}
        statement_error: asyncpg.PostgresError | None = None
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                admin_bypass = await _admin_bypass(conn, statement)
                if statement.check_policy and not (read_only and should_fetch):
                    # A read-only statement writes nothing, so it holds no
                    # Vault for the policy while it runs.
                    denial = await _check_vault_write_policy(
                        conn, statement.vault_names, statement.user_id,
                        admin_bypass=admin_bypass, overrides=overrides,
                        lock=not read_only, admitted=admitted,
                    )
                    if denial:
                        if not should_fetch:
                            return denial
                        # A row-returning statement can have side effects. Keep
                        # reads available and let PostgreSQL enforce the policy.
                        read_only = True
                await _enter_statement_role(conn, statement, admin_bypass)

                if read_only:
                    await conn.execute("SET TRANSACTION READ ONLY")

                if not overrides:
                    result = await _run_statement(
                        conn, statement.sql, statement.params, should_fetch, statement.vault_names,
                    )
                    if not read_only:
                        await _check_row_changes_admitted(conn)
                    return result
                # Keep the override audit event even if PostgreSQL rejects
                # the user's statement. Only the statement rolls back, and
                # the event is inserted after it, as the transaction's last
                # locking statement.
                try:
                    async with conn.transaction():
                        outcome = await _run_statement(
                            conn, statement.sql, statement.params, should_fetch,
                            statement.vault_names,
                        )
                except asyncpg.PostgresError as exc:
                    statement_error = exc
                if not read_only:
                    await _check_row_changes_admitted(conn)
                    await overrides.flush(conn)
        if read_only:
            # A read-only transaction cannot insert the override event. Its
            # statement wrote nothing, so the event commits alone, admitted
            # for its Vaults like any event write, on a connection taken after
            # the statement's was given back.
            async with vault_write_admission(*overrides.vault_ids()), self.pool.acquire() as conn:
                async with conn.transaction():
                    await overrides.flush(conn)
        if statement_error is not None:
            raise statement_error
        return outcome

    async def _declared(
        self, statement: _Statement, vault_ids: Optional[list[uuid.UUID]],
    ) -> frozenset[uuid.UUID]:
        if vault_ids is not None:
            return frozenset(vault_ids)
        return frozenset(await self._vault_ids(statement.vault_names))

    async def _vault_names(self, vault_ids: frozenset[uuid.UUID]) -> list[str]:
        """The names of the given Vaults, ids where a Vault has none any more."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name FROM vaults WHERE id = ANY($1::uuid[]) ORDER BY name", list(vault_ids),
            )
        named = {row["id"] for row in rows}
        return [row["name"] for row in rows] + sorted(str(v) for v in vault_ids - named)

    async def _vault_ids(self, vault_names: list[str]) -> list[uuid.UUID]:
        """The ids of the named Vaults, read before the statement's transaction."""
        if not vault_names:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id FROM vaults WHERE name = ANY($1::text[])", vault_names)
        return [row["id"] for row in rows]


@dataclass(frozen=True)
class _Statement:
    user_id: uuid.UUID | str
    actor_id: str
    sql: str
    params: list[Any]
    fetch: bool
    is_admin: bool
    vault_names: list[str]
    check_policy: bool


class UndeclaredVaultWriteError(Exception):
    """The statement writes dynamic-table rows of Vaults the request did not
    declare. Either its plan shows it, and it was refused before it ran
    (``ran`` False), or a function the statement calls did, out of the plan's
    sight, and its transaction was rolled back."""

    def __init__(self, vaults: list[str], *, ran: bool = True) -> None:
        names = ", ".join(vaults)
        outcome = "nothing was committed" if ran else "it was not run"
        verb = "changed rows of" if ran else "writes"
        super().__init__(
            f"The statement {verb} Vault(s) {names}, which the request did not declare; "
            f"{outcome}. Declare every Vault it writes in vaults and send it again.",
        )
        self.vaults = vaults


class _PlanWaiter:
    __slots__ = ("vaults", "granted")

    def __init__(self, vaults: frozenset[str], granted: asyncio.Future) -> None:
        self.vaults = vaults
        self.granted = granted


class _PlanSlots:
    """The plan readers of one event loop.

    At most `akb_sql_plan_concurrency` plans are read at once, and at most
    one per Vault: a plan holds a token for every Vault its statement
    declares, taken all at once or not at all, as the Vault write admission
    takes its slots. Waiters hold nothing and are served first come, first
    served, by capacity (``grant``), as in the Vault write admission: a
    waiter whose Vaults are not all free reserves their tokens against the
    waiters behind it, which take the slots and the other Vaults' tokens
    left. Plans of a Vault whose table is locked therefore wait one behind
    another, whatever other Vaults they declare with it, a statement of a
    Vault no earlier waiter declares takes the next free slot, and
    single-Vault statements cannot keep a statement of several Vaults
    waiting until its deadline by taking its Vaults in turn.
    """

    def __init__(self) -> None:
        self.running = 0
        self.busy: set[str] = set()
        self.waiters: list[_PlanWaiter] = []

    def take(self, vaults: frozenset[str]) -> None:
        self.running += 1
        self.busy |= vaults

    def give_back(self, vaults: frozenset[str], limit: int) -> None:
        self.running -= 1
        self.busy -= vaults
        self.grant(limit)

    def grant(self, limit: int) -> None:
        """Start the waiters that fit, in arrival order.

        Called whenever a slot is given back or a waiter leaves, and for each
        new waiter. A waiter starts when a slot is free and none of its
        Vaults is busy or reserved by a waiter before it; otherwise it
        reserves its Vaults and the walk goes on.
        """
        reserved: set[str] = set()
        for waiter in list(self.waiters):
            if waiter.granted.done():
                self.waiters.remove(waiter)
            elif self.running >= limit:
                break
            elif self.busy.isdisjoint(waiter.vaults) and reserved.isdisjoint(waiter.vaults):
                self.take(waiter.vaults)
                waiter.granted.set_result(None)
                self.waiters.remove(waiter)
            else:
                reserved |= waiter.vaults


# Plan readers per event loop (tests run each in a loop of its own).
_plan_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _PlanSlots] = (
    weakref.WeakKeyDictionary()
)


@asynccontextmanager
async def _plan_slot(vaults: list[str]) -> AsyncIterator[None]:
    """Wait for one of `akb_sql_plan_concurrency` plan readers and for the
    plan tokens of the declared ``vaults``, holding no connection, for at
    most `vault_write_queue_timeout_secs`. A wait that ends without them,
    refused or cancelled, leaves the queue at once, and the waiters its
    reservation held back go on."""
    loop = asyncio.get_running_loop()
    slots = _plan_slots.get(loop)
    if slots is None:
        slots = _plan_slots[loop] = _PlanSlots()
    limit = max(1, settings.akb_sql_plan_concurrency)
    timeout = settings.vault_write_queue_timeout_secs
    wanted = frozenset(vaults)
    waiter = _PlanWaiter(wanted, loop.create_future())
    slots.waiters.append(waiter)
    slots.grant(limit)
    if not waiter.granted.done():
        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(waiter.granted)
        except BaseException as exc:
            if waiter.granted.done():
                # Granted as the wait ended: give the tokens back.
                slots.give_back(wanted, limit)
            else:
                # Leave the queue, and start the waiters its reservation
                # kept waiting.
                waiter.granted.cancel()
                slots.waiters.remove(waiter)
                slots.grant(limit)
            if isinstance(exc, TimeoutError):
                raise WriteBusyError(", ".join(vaults) or "akb_sql", timeout, retry_after_secs=1) from None
            raise
    try:
        yield
    finally:
        slots.give_back(wanted, limit)


async def _admin_bypass(conn, statement: _Statement, *, lock: bool = True) -> bool:
    """Whether a live unscoped human administrator keeps the backend role."""
    return current_vault_scope.get() is None and await human_admin_sql_bypass(
        conn, statement.user_id, statement.is_admin, lock=lock,
    )


async def _enter_statement_role(conn, statement: _Statement, admin_bypass: bool) -> None:
    """Take the caller's role and settings for the rest of the transaction."""
    scope = current_vault_scope.get()
    token_id = current_token_id.get()
    if scope is not None and token_id is not None:
        # Scoped PAT → the narrow per-token role (membership =
        # owner-ACL ∩ scope). Precedence over is_admin: a
        # SCOPED admin token does NOT bypass — PG confines its
        # akb_sql to the scope (42501 outside). If the role is
        # missing (a best-effort provisioning hook failed
        # before the reconciler ran), this SET raises →
        # fail-CLOSED: we never fall back to the full
        # akb_user_<uid>, which would defeat the scope.
        role = token_role_name(token_id)
        await conn.execute(f'SET LOCAL ROLE "{role}"')
    elif not admin_bypass:
        role = user_role_name(statement.user_id)
        # asyncpg passes the role name as an identifier here;
        # we built it ourselves from a UUID so the only chars
        # are [a-z0-9_]. No injection risk.
        await conn.execute(f'SET LOCAL ROLE "{role}"')
    # else: live unscoped human admin → connection default.
    # Reset search_path defensively — a previous tx on this
    # pooled connection might have left a different default
    # (it shouldn't, but be explicit).
    await conn.execute("SET LOCAL search_path = public")
    await conn.execute(
        "SELECT set_config('akb.actor_id', $1, true)",
        statement.actor_id,
    )
    request_claims = current_request_jwt_claims.get()
    if request_claims is not None:
        await conn.execute(
            "SELECT set_config('request.jwt.claims', $1, true)",
            request_claims.to_json(),
        )


# The Vault of each dynamic table is the first argument of its row-change
# triggers (`table_data_repo.create_dynamic_table`); those triggers queue the
# table's `table.rows_changed` events for that Vault.
_VAULTS_OF_TABLES = r"""
SELECT DISTINCT split_part(encode(t.tgargs, 'escape'), '\000', 1) AS vault_id
  FROM pg_catalog.pg_trigger AS t
  JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
  JOIN unnest($1::text[], $2::text[]) AS m(schema_name, table_name)
    ON n.nspname = m.schema_name AND c.relname = m.table_name
 WHERE t.tgname IN (
     'akb_rows_changed_insert_trigger',
     'akb_rows_changed_update_trigger',
     'akb_rows_changed_delete_trigger'
 )
"""


def _modified_relations(plan: Any) -> set[tuple[str, str]]:
    """(schema, relation) of every ModifyTable node in an EXPLAIN JSON plan."""
    found: set[tuple[str, str]] = set()
    stack = [plan]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            if node.get("Node Type") == "ModifyTable" and node.get("Relation Name"):
                found.add((node.get("Schema") or "public", node["Relation Name"]))
            stack.extend(node.values())
    return found


async def _check_row_changes_admitted(conn) -> None:
    """Raise ``UndeclaredVaultWriteError`` when the transaction queued row
    changes of a Vault it is not admitted for.

    Dynamic tables queue their row changes, and the events are inserted, and
    the Vault's event lock taken, only when the transaction commits
    (migration 132). The queue holds no committed rows (each is deleted as
    its event is inserted), so the rows this transaction sees are its own.
    No statement `akb_sql` accepts can create a function or change when
    those events are inserted (see `table_service.execute_sql`), so this
    check is a backstop for functions an administrator installed.
    """
    # The queue is read under the backend role.
    await conn.execute("SET LOCAL ROLE NONE")
    rows = await conn.fetch("SELECT DISTINCT vault_id FROM public.events_row_change_queue")
    unadmitted = sorted(row["vault_id"] for row in rows if not is_admitted(row["vault_id"]))
    if unadmitted:
        names = await conn.fetch(
            "SELECT name FROM vaults WHERE id = ANY($1::uuid[]) ORDER BY name", unadmitted,
        )
        raise UndeclaredVaultWriteError([r["name"] for r in names] or [str(v) for v in unadmitted])


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
