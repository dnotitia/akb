"""Notification eligibility and identity at the transactional source seam."""
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.repositories.events_repo import emit_event
from app.services import notification_producer as producer


def fact(kind="access.grant", **changes):
    payload = dict(applied=True, target_user_id=str(uuid.uuid4()),
                   previous_effective_role=None, effective_role="reader", public_access="none")
    payload.update(changes)
    return producer.access_notification(kind, payload)


@pytest.mark.parametrize("changes", [
    {"applied": False},
    {"previous_effective_role": "reader"},
    {"public_access": "writer"},
    {"target_is_owner": True},
    {"target_is_admin": True},
])
def test_grant_without_effective_change_is_silent(changes):
    assert fact(**changes) is None


@pytest.mark.parametrize("public,remaining,kind", [
    ("none", None, "access.revoked"),
    ("reader", None, "access.membership_removed"),
    ("none", "writer", "access.membership_removed"),
])
def test_direct_membership_loss_distinguishes_surviving_access(public, remaining, kind):
    result = fact("access.revoke", previous_effective_role="writer", effective_role=remaining,
                  direct_removed=True, public_access=public)
    assert result[0] == kind
    assert result[2]["role"] == (remaining or (None if public == "none" else public))


def test_non_direct_basis_noop_is_silent_but_downgrade_is_not():
    assert fact("access.revoke", previous_effective_role="reader") is None
    assert fact("access.revoke", previous_effective_role="writer")[0] == "access.changed"


def test_transfer_targets_actual_previous_and_new_owner_not_operator():
    before, after = str(uuid.uuid4()), str(uuid.uuid4())
    assert producer.access_notification("access.transfer_ownership", {
        "from_user_id": before, "to_user_id": after,
    }) == ("access.ownership", [before, after], {})
    assert producer.access_notification("access.transfer_ownership", {
        "from_user_id": before, "to_user_id": before,
    }) is None


@pytest.mark.parametrize("before,after,expected", [
    ("draft", "archived", "document.archive"),
    ("archived", "active", "document.restore"),
    ("archived", "archived", "document.update"),
    (None, None, "document.update"),
])
def test_archive_transition_uses_both_states(before, after, expected):
    assert producer.document_notification_kind("document.update", before, after) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["document.update", "document.move", "document.delete"])
async def test_legacy_events_queue_same_connection_uuid_and_minimal_payload(monkeypatch, kind):
    resource, actual_actor, username = uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    conn = AsyncMock()
    conn.fetchval.side_effect = [123, actual_actor]
    enqueue = AsyncMock()
    monkeypatch.setattr(producer, "enqueue_notification_event", enqueue)
    assert await emit_event(conn, kind, vault_id=uuid.uuid4(), actor_id=username, payload={
        "resource_id": str(resource), "path": "private/path.md", "title": "private title",
    }) == 123
    assert conn.fetchval.call_args.args == ("SELECT id FROM users WHERE username = $1", username)
    assert enqueue.call_args.args == (conn, kind)
    kwargs = enqueue.call_args.kwargs
    assert kwargs["resource_id"] == str(resource)
    assert kwargs["actor_id"] == str(actual_actor)
    assert kwargs["source_key"] == "event:123"
    assert "payload" not in kwargs


@pytest.mark.asyncio
async def test_enqueue_failure_propagates_to_domain_transaction(monkeypatch):
    conn = AsyncMock()
    conn.fetchval.return_value = 123
    monkeypatch.setattr(producer, "enqueue_notification_event", AsyncMock(side_effect=RuntimeError("queue failed")))
    with pytest.raises(RuntimeError, match="queue failed"):
        await emit_event(conn, "document.delete", vault_id=uuid.uuid4(), payload={"resource_id": str(uuid.uuid4())})


@pytest.mark.asyncio
async def test_unrelated_and_create_events_do_not_queue(monkeypatch):
    enqueue = AsyncMock()
    monkeypatch.setattr(producer, "enqueue_notification_event", enqueue)
    for kind in ("document.put", "table.update", "vault.create"):
        await producer.enqueue_domain_event(AsyncMock(), 1, kind, vault_id=uuid.uuid4(), actor_id=None,
                                            payload={"resource_id": str(uuid.uuid4())})
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["document", "file"])
@pytest.mark.parametrize("operation", ["replace", "delete"])
async def test_native_publication_enqueues_before_commit_on_authority_connection(monkeypatch, surface, operation):
    from app.services import native_revision_service as native
    in_transaction = False
    @asynccontextmanager
    async def transaction():
        nonlocal in_transaction
        in_transaction = True
        try:
            yield
        finally:
            in_transaction = False
    conn = SimpleNamespace(transaction=transaction)
    @asynccontextmanager
    async def acquire():
        yield conn
    resource_id, vault_id = uuid.uuid4(), uuid.uuid4()
    repository = AsyncMock()
    repository.find_mutation.return_value = None
    service = native.NativeRevisionService(SimpleNamespace(acquire=acquire), repository=repository, payload_store=object())
    monkeypatch.setattr(service, "_allocate_revision_id", AsyncMock(return_value="r-new"))
    monkeypatch.setattr(service, "_lock_live_reference", AsyncMock(return_value={
        "current_path": "renamed.md", "head_revision_id": "r-old", "resource_id": resource_id,
    }))
    observed = []
    async def enqueue(connection, kind, **kwargs):
        assert connection is conn and in_transaction
        observed.append((kind, kwargs))
    monkeypatch.setattr(native, "enqueue_document_change", enqueue)
    kwargs = dict(namespace_id=vault_id, surface=surface, path="old.md", actor="writer",
                  mutation_id=uuid.uuid4(), expected_revision_id="r-old", expected_resource_id=resource_id,
                  message=None, subject=None, summary=None, fingerprint="fingerprint")
    if operation == "replace":
        kwargs.update(prepared=object(), notification_previous_status="active", notification_status="archived")
    await getattr(service, f"_publish_{operation}")(**kwargs)
    assert not in_transaction
    if surface == "file":
        assert observed == []
    else:
        kind, sent = observed[0]
        assert kind == ("document.update" if operation == "replace" else "document.delete")
        assert sent["resource_id"] == resource_id
        assert sent["vault_id"] == vault_id
        if operation == "replace":
            assert sent["previous_status"] == "active" and sent["status"] == "archived"
