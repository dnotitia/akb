"""A Bare Git document write that cannot get its Vault's event lock fails
before it commits to Git.

A Git commit does not roll back with the database transaction. The writes used
to commit to Git and only then insert their event. When the Vault's event lock
stayed busy past its deadline, the transaction rolled back and the API answered
with a retryable ``write_busy``, while Git kept the commit: Git and the
database then disagreed about the document, and a later metadata-only update,
which reads the body from Git, could publish the body of the failed write.
Each Bare Git write now takes the lock before its Git commit.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from app.db.transient import transient_error
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.services.document_service import DocumentService
from app.services.git_service import GitService
from app.util.errors import WRITE_BUSY
from tests.test_events_commit_order_postgres import (
    _EVENT_LOCK_WAIT_SECONDS,
    _OpenTransaction,
    _emit,
    _fresh_database,
    _make_vault,
)

pytestmark = pytest.mark.asyncio

_OPERATIONS = ("put", "update", "edit", "move", "delete")


async def _write(service: DocumentService, operation: str, vault: str, path: str, head: str) -> None:
    if operation == "put":
        await service.put(DocumentPutRequest(
            vault=vault, collection="notes", title="Second", content="second body",
        ))
    elif operation == "update":
        await service.update(vault, path, DocumentUpdateRequest(
            content="changed body", expected_commit=head,
        ))
    elif operation == "edit":
        await service.edit(vault, path, "first body", "edited body")
    elif operation == "move":
        await service.move(vault, path, collection="moved")
    else:
        await service.delete(vault, path)


async def test_a_busy_vault_fails_each_git_write_before_git_changes(tmp_path):
    git = GitService(storage_path=str(tmp_path))
    service = DocumentService(git=git)
    async with _fresh_database() as pool:
        prepared = {}
        for operation in _OPERATIONS:
            vault = f"git-lock-{operation}"
            vault_id = await _make_vault(pool, vault)
            git.init_vault(vault)
            # This first write also creates the Vault's lock row, as on any
            # Vault that already has events.
            doc = await service.put(DocumentPutRequest(
                vault=vault, collection="notes", title="First", content="first body",
            ))
            documents = await pool.fetch(
                "SELECT path, current_commit FROM documents WHERE vault_id = $1", vault_id,
            )
            events = await pool.fetchval("SELECT count(*) FROM events WHERE vault_id = $1", vault_id)
            prepared[operation] = (vault, vault_id, doc.path, git.current_commit(vault), documents, events)

        async def attempt(operation: str) -> tuple[BaseException | None, float]:
            vault, _, path, head, *_ = prepared[operation]
            started = time.monotonic()
            try:
                await _write(service, operation, vault, path, head)
            except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
                return exc, time.monotonic() - started
            return None, time.monotonic() - started

        async with contextlib.AsyncExitStack() as stack:
            for operation in _OPERATIONS:
                holder = await stack.enter_async_context(pool.acquire())
                await stack.enter_async_context(_OpenTransaction(holder))
                await _emit(holder, prepared[operation][1], "holder")
            results = await asyncio.wait_for(
                asyncio.gather(*(attempt(operation) for operation in _OPERATIONS)),
                timeout=_EVENT_LOCK_WAIT_SECONDS * 4,
            )
        # The holders rolled back; every Vault is as the first write left it.

        outcomes = {}
        for operation, (error, waited) in zip(_OPERATIONS, results):
            vault, vault_id, _, head, documents, events = prepared[operation]
            mapped = transient_error(error) if isinstance(error, Exception) else None
            outcomes[operation] = {
                "error": type(error).__name__ if error is not None else None,
                "api": (mapped.status_code, mapped.code) if mapped is not None else None,
                "waited for the deadline": waited >= _EVENT_LOCK_WAIT_SECONDS - 0.5,
                "git head unchanged": git.current_commit(vault) == head,
                "documents unchanged": await pool.fetch(
                    "SELECT path, current_commit FROM documents WHERE vault_id = $1", vault_id,
                ) == documents,
                "no new event": await pool.fetchval(
                    "SELECT count(*) FROM events WHERE vault_id = $1", vault_id,
                ) == events,
            }
        expected = {
            "error": "LockNotAvailableError",
            "api": (429, WRITE_BUSY),
            "waited for the deadline": True,
            "git head unchanged": True,
            "documents unchanged": True,
            "no new event": True,
        }
        assert outcomes == {operation: expected for operation in _OPERATIONS}
