"""DB-free contracts for legacy adoption normalization and ownership fences."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from app.exceptions import ConflictError, ForbiddenError, ValidationError
from app.services import app_legacy_adoption_service as adoption
from app.services import app_resource_service as resources
from app.services.auth_service import AuthenticatedUser


def _user(*, is_admin: bool = False) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="operator",
        email="operator@example.invalid",
        display_name=None,
        is_admin=is_admin,
        auth_method="jwt",
    )


def _target(vault_id: uuid.UUID, *, tables: list[str] | None = None) -> dict:
    return {
        "vault_id": str(vault_id),
        "table_allowlist": tables or ["orders"],
        "expected_schema_fingerprint": "A" * 64,
    }


def test_allowlist_is_sorted_deduplicated_and_rejects_non_arrays() -> None:
    vault_a = uuid.uuid4()
    vault_b = uuid.uuid4()
    normalized = adoption.normalize_adoption_targets(
        [
            _target(vault_b, tables=["zeta", "alpha", "zeta"]),
            _target(vault_a, tables=["orders"]),
        ]
    )

    assert [item["vault_id"] for item in normalized] == sorted(
        [str(vault_a), str(vault_b)]
    )
    by_vault = {item["vault_id"]: item for item in normalized}
    assert by_vault[str(vault_b)]["table_allowlist"] == ["alpha", "zeta"]
    assert by_vault[str(vault_b)]["expected_schema_fingerprint"] == "a" * 64

    with pytest.raises(ValidationError):
        adoption.normalize_adoption_targets(
            [{"vault_id": str(vault_a), "table_allowlist": "orders", "expected_schema_fingerprint": "a" * 64}]
        )
    with pytest.raises(ValidationError):
        adoption.normalize_adoption_targets(
            [_target(vault_a), _target(vault_a)]
        )


def test_digest_is_canonical_over_target_order_and_json_key_order() -> None:
    app_id = uuid.uuid4()
    release_id = uuid.uuid4()
    first = adoption.normalize_adoption_targets(
        [_target(uuid.UUID(int=2), tables=["orders", "users"])]
    )
    second = adoption.normalize_adoption_targets(
        [
            {
                "expected_schema_fingerprint": "a" * 64,
                "table_allowlist": ["users", "orders", "orders"],
                "vault_id": str(uuid.UUID(int=2)),
            }
        ]
    )

    payload_a, digest_a = adoption.adoption_input_digest(app_id, release_id, first)
    payload_b, digest_b = adoption.adoption_input_digest(app_id, release_id, second)
    assert payload_a == payload_b
    assert digest_a == digest_b
    assert digest_a == hashlib.sha256(adoption.canonical_json(payload_a)).hexdigest()
    assert json.loads(adoption.canonical_json(payload_a))["targets"][0]["table_allowlist"] == [
        "orders",
        "users",
    ]


def test_fingerprint_is_allowlist_scoped_and_order_independent() -> None:
    rows = [
        {
            "name": "orders",
            "columns": [{"name": "amount", "type": "numeric"}],
            "unique_keys": [],
            "indexes": [],
        },
        {
            "name": "users",
            "columns": [{"name": "email", "type": "text"}],
            "unique_keys": [{"name": "users_email_uk", "columns": ["email"]}],
            "indexes": [],
        },
    ]
    expected = resources.canonical_table_fingerprint(rows)
    assert expected == resources.canonical_table_fingerprint(list(reversed(rows)))
    assert expected == hashlib.sha256(
        resources.canonical_json(
            [
                {
                    "name": "orders",
                    "columns": [{"name": "amount", "type": "numeric"}],
                    "unique_keys": [],
                    "indexes": [],
                },
                {
                    "name": "users",
                    "columns": [{"name": "email", "type": "text"}],
                    "unique_keys": [{"name": "users_email_uk", "columns": ["email"]}],
                    "indexes": [],
                },
            ]
        )
    ).hexdigest()


class _OwnershipConnection:
    def __init__(self, row: dict | None):
        self.row = row

    async def fetchrow(self, _query: str, *_args):
        return self.row


@pytest.mark.asyncio
async def test_owned_table_requires_exact_rollout_context(monkeypatch) -> None:
    installation_id = uuid.uuid4()
    app_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    row = {
        "installation_id": installation_id,
        "app_id": app_id,
        "status": "owned",
    }
    conn = _OwnershipConnection(row)

    with pytest.raises(ConflictError):
        await resources.ensure_table_mutation_allowed(conn, vault_id, "orders")
    with pytest.raises(ConflictError):
        await resources.ensure_table_mutation_allowed(
            conn,
            vault_id,
            "orders",
            context=resources.TableOwnershipContext(uuid.uuid4(), app_id),
        )
    with pytest.raises(ConflictError):
        await resources.ensure_table_mutation_allowed(
            conn,
            vault_id,
            "orders",
            context=resources.TableOwnershipContext(installation_id, uuid.uuid4()),
        )

    await resources.ensure_table_mutation_allowed(
        conn,
        vault_id,
        "orders",
        context=resources.TableOwnershipContext(installation_id, app_id),
    )


@pytest.mark.asyncio
async def test_retained_table_never_accepts_rollout_context() -> None:
    conn = _OwnershipConnection(
        {
            "installation_id": uuid.uuid4(),
            "app_id": uuid.uuid4(),
            "status": "retained",
        }
    )
    with pytest.raises(ConflictError):
        await resources.ensure_table_mutation_allowed(
            conn,
            uuid.uuid4(),
            "orders",
            context=resources.TableOwnershipContext(uuid.uuid4(), uuid.uuid4()),
        )


@pytest.mark.asyncio
async def test_target_authorization_denies_before_app_lookup(monkeypatch) -> None:
    class _Pool:
        def acquire(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def fetch(self, _query, target_ids):
            return [{"id": target_ids[0], "name": "target-vault"}]

    called = False

    async def check(*_args, **_kwargs):
        nonlocal called
        called = True
        raise ForbiddenError("denied")

    async def pool():
        return _Pool()

    monkeypatch.setattr(adoption, "get_pool", pool)
    monkeypatch.setattr(adoption, "check_vault_access", check)

    with pytest.raises(ForbiddenError, match="Legacy adoption request denied"):
        await adoption._authorize_target_vaults(
            _user(),
            [_target(uuid.uuid4())],
            app_id=uuid.uuid4(),
            action="app.legacy_adoption.create",
            correlation_id="unit-test",
        )
    assert called is True
