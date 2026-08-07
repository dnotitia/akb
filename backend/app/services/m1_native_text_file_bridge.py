"""Atomic native-ledger bridge for guarded M1 searchable text Files.

The public File service owns the outer PostgreSQL transaction.  Native ledger
publication must therefore reuse that exact connection: committing a separate
ledger transaction before ``vault_files`` is visible would create two
authorities after a failure.  ``_BoundPool`` adapts the existing native service
and PostgreSQL BodyStore to a caller-owned connection; their nested
transactions become asyncpg savepoints and the outer File transaction remains
the only commit boundary.

Because the confirm is a *composite* transaction, this bridge is also the only
place a C5 failpoint can be injected where the risk actually lives.  A failure
raised inside the nested ledger publish must unwind the savepoint *and* the
outer ``vault_files`` transaction, leaving no public File row, no ledger rows
and no orphan visible state.  ``install_m1_native_text_file_bridge`` therefore
accepts the same deterministic test-only hook the native service documents;
production installation leaves it unset.
"""

from __future__ import annotations

import uuid
from functools import partial
from typing import Any, cast

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import AKBError
from app.repositories.native_revision_repo import NativeRevisionRepository
from app.services.m1_file_measurement import (
    NativeTextDeleteRequest,
    NativeTextOpenResult,
    NativeTextPublication,
    NativeTextPublishRequest,
    register_native_text_file_services,
)
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_service import Failpoint, NativeRevisionService


_MUTATION_NAMESPACE = uuid.UUID("8d881f1f-72a0-4bd6-9c06-f02a5cc19c33")


class _BoundAcquire:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self.conn

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _BoundPool:
    """The narrow ``Pool.acquire`` shape used by native M1 services."""

    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    def acquire(self) -> _BoundAcquire:
        return _BoundAcquire(self.conn)


def _service_on(
    conn: asyncpg.Connection,
    failpoint: Failpoint | None = None,
) -> NativeRevisionService:
    # The composed services only call Pool.acquire(); every call must resolve
    # to the transaction-owning connection above.  The casts are limited to
    # this measurement-only adapter instead of weakening production types.
    pool = cast(asyncpg.Pool, cast(Any, _BoundPool(conn)))
    return NativeRevisionService(
        pool,
        repository=NativeRevisionRepository(pool),
        payload_store=M1PgBodyStore(pool),
        failpoint=failpoint,
    )


def _mutation_id(kind: str, *parts: object) -> uuid.UUID:
    return uuid.uuid5(_MUTATION_NAMESPACE, "\0".join((kind, *(str(part) for part in parts))))


async def _publish(
    conn: object,
    request: NativeTextPublishRequest,
    *,
    failpoint: Failpoint | None = None,
) -> NativeTextPublication:
    if not isinstance(conn, asyncpg.Connection):
        raise AKBError("native text File publisher requires a PostgreSQL transaction", status_code=503)
    result = await _service_on(conn, failpoint).create_text(
        namespace_id=request.vault_id,
        surface="file",
        path=request.logical_path,
        payload=request.data,
        actor=request.actor_id,
        mutation_id=_mutation_id(
            "publish",
            request.file_id,
            request.logical_path,
            request.digest,
            request.actor_id,
            request.description,
        ),
        resource_id=request.file_id,
        message=request.description or "File upload",
        expected_digest=request.digest,
        expected_size=len(request.data),
    )
    return NativeTextPublication(
        resource_id=result.resource_id,
        revision_id=result.revision_id,
        digest=request.digest,
        size_bytes=len(request.data),
    )


async def _open(
    vault_id: uuid.UUID,
    resource_id: uuid.UUID,
    revision_id: str,
) -> NativeTextOpenResult:
    pool = await get_pool()
    snapshot = await NativeRevisionService(
        pool,
        payload_store=M1PgBodyStore(pool),
    ).get_resource_revision(
        namespace_id=vault_id,
        surface="file",
        resource_id=resource_id,
        revision_id=revision_id,
    )
    return NativeTextOpenResult(
        data=snapshot.payload_bytes,
        digest=snapshot.digest,
        size_bytes=snapshot.byte_size,
    )


async def _delete(
    conn: object,
    request: NativeTextDeleteRequest,
    *,
    failpoint: Failpoint | None = None,
) -> None:
    if not isinstance(conn, asyncpg.Connection):
        raise AKBError("native text File deleter requires a PostgreSQL transaction", status_code=503)
    result = await _service_on(conn, failpoint).delete_resource(
        namespace_id=request.vault_id,
        surface="file",
        path=request.logical_path,
        actor=request.actor_id,
        mutation_id=_mutation_id(
            "delete",
            request.resource_id,
            request.revision_id,
            request.logical_path,
            request.actor_id,
        ),
        expected_revision_id=request.revision_id,
        expected_resource_id=request.resource_id,
        message="File delete",
    )
    if result.resource_id != request.resource_id or result.parent_revision_id != request.revision_id:
        raise AKBError("native text File delete returned the wrong lineage", status_code=502)


def install_m1_native_text_file_bridge(*, failpoint: Failpoint | None = None) -> None:
    """Install the guarded process callbacks after migrations have completed.

    ``failpoint`` is the native service's deterministic test-only hook; it is
    bound onto the two callbacks that publish authority inside the caller's
    File transaction.  Production installation must leave it unset, and unset
    it composes the same ``NativeRevisionService`` as before.  ``_open`` is
    deliberately excluded: it reads on its own pool connection outside any
    File transaction and crosses no authority boundary.
    """
    register_native_text_file_services(
        publisher=partial(_publish, failpoint=failpoint),
        opener=_open,
        deleter=partial(_delete, failpoint=failpoint),
    )
