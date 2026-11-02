"""Realm-account residue: the set arithmetic, the read, and its failure path.

The reconciliation is ``realm accounts - (exact bindings | recorded arrivals)``.
Two properties are asserted here rather than assumed:

* the read never writes: the Keycloak adapter issues only the token request and
  ``GET`` user listings, and the route has no mutation dependency;
* a failed realm read is an explicit ``unavailable`` state with no counts. It
  must never be rendered as zero residue, which would tell an administrator
  that nothing has accumulated when nothing was measured.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from app.api.routes import admin_auth, admin_sso
from app.config import settings
from app.models.admin_sso import (
    SsoRealmAccountResidueResponse,
    SsoRealmArrivalView,
    SsoRealmResidueAccountView,
)
from app.services.admin_auth_service import ProductAdminIdentity
from app.sso import realm_residue
from app.sso.keycloak_admin import (
    KeycloakAdminConfig,
    KeycloakProviderControl,
    ProviderControlError,
)
from app.sso.models import RealmAccount, RealmAccountListing
from app.sso.realm_residue import ArrivalRecord, reconcile


_ISSUER = "https://sso.example.test/realms/akb"


def _at(day: int) -> datetime:
    return datetime(2026, 10, day, 9, 0, tzinfo=timezone.utc)


def _account(user_id: str, day: int | None) -> RealmAccount:
    return RealmAccount(
        user_id=user_id,
        username=f"user-{user_id}",
        created_at=_at(day) if day is not None else None,
    )


def _arrival(day: int) -> ArrivalRecord:
    return ArrivalRecord(admission_id=str(uuid.uuid4()), last_seen_at=_at(day))


# ── the set arithmetic ────────────────────────────────────────────────────────


def test_residue_is_realm_accounts_minus_bindings_and_recorded_arrivals():
    listing = RealmAccountListing(
        accounts=(
            _account("bound-1", 1),
            _account("bound-2", 2),
            _account("pending", 3),
            _account("residue-new", 9),
            _account("residue-old", 4),
        ),
        truncated=False,
    )
    pending = _arrival(5)
    result = reconcile(
        listing,
        bound_subjects=frozenset({"bound-1", "bound-2", "bound-elsewhere"}),
        arrivals={"pending": pending, "arrival-elsewhere": _arrival(6)},
        list_limit=200,
    )

    # A binding or an arrival for a subject the realm no longer lists changes
    # no count: only realm accounts are reconciled.
    assert result.realm_account_count == 5
    assert result.bound_count == 2
    assert result.pending_arrival_count == 1
    assert result.residue_count == 2
    assert result.truncated is False
    # Residue first and oldest first; the pending arrival after it, carrying
    # its record. Bound accounts are never listed.
    assert [(item.account.user_id, item.arrival) for item in result.accounts] == [
        ("residue-old", None),
        ("residue-new", None),
        ("pending", pending),
    ]


def test_an_account_both_bound_and_recorded_counts_as_bound_once():
    listing = RealmAccountListing(accounts=(_account("both", 1),), truncated=False)
    result = reconcile(
        listing,
        bound_subjects=frozenset({"both"}),
        arrivals={"both": _arrival(2)},
        list_limit=200,
    )
    assert (result.bound_count, result.pending_arrival_count, result.residue_count) == (1, 0, 0)
    assert result.accounts == ()


def test_an_unknown_creation_time_sorts_after_known_ones_and_never_raises():
    listing = RealmAccountListing(
        accounts=(_account("unknown", None), _account("known", 3)),
        truncated=False,
    )
    result = reconcile(listing, bound_subjects=frozenset(), arrivals={}, list_limit=200)
    assert [item.account.user_id for item in result.accounts] == ["known", "unknown"]


def test_the_list_is_bounded_but_the_counts_are_not():
    listing = RealmAccountListing(
        accounts=tuple(_account(f"r{index:02d}", index + 1) for index in range(5)),
        truncated=True,
    )
    result = reconcile(
        listing,
        bound_subjects=frozenset(),
        arrivals={"r00": _arrival(9)},
        list_limit=3,
    )
    assert result.residue_count == 4
    assert result.pending_arrival_count == 1
    assert result.truncated is True
    # The bound keeps residue ahead of pending arrivals.
    assert [item.account.user_id for item in result.accounts] == ["r01", "r02", "r03"]


# ── the orchestration and its failure path ───────────────────────────────────


class _Control:
    def __init__(self, *, mode: str = "direct", error: str | None = None,
                 listing: RealmAccountListing | None = None,
                 issuer: str = _ISSUER) -> None:
        self.control_mode = mode
        self.error = error
        self.listing = listing or RealmAccountListing(accounts=(), truncated=False)
        self.config = type("Config", (), {"broker_issuer": issuer})()
        self.limits: list[int] = []

    async def list_realm_accounts(self, *, limit: int) -> RealmAccountListing:
        self.limits.append(limit)
        if self.error is not None:
            raise ProviderControlError(self.error)
        return self.listing


@pytest.fixture
def sso_settings(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://sso.example.test", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    assert settings.keycloak_issuer == _ISSUER


@pytest.fixture
def no_database(monkeypatch):
    async def refuse(*_args, **_kwargs):
        raise AssertionError("an unavailable realm read must not reach the database")

    monkeypatch.setattr(realm_residue, "read_akb_records", refuse)


def _unavailable(code: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "state": "unavailable",
        "unavailable_code": code,
        "scan_limit": realm_residue.REALM_ACCOUNT_SCAN_LIMIT,
        "truncated": None,
        "realm_account_count": None,
        "bound_count": None,
        "pending_arrival_count": None,
        "residue_count": None,
        "accounts": None,
    }


@pytest.mark.asyncio
async def test_a_failed_realm_read_is_unavailable_never_zero(sso_settings, no_database):
    control = _Control(error="keycloak_provider_control_unreachable")
    payload = await realm_residue.realm_account_residue(control)
    assert payload == _unavailable("keycloak_provider_control_unreachable")
    assert control.limits == [realm_residue.REALM_ACCOUNT_SCAN_LIMIT]


@pytest.mark.asyncio
async def test_a_delegated_installation_cannot_read_and_says_so(sso_settings, no_database):
    control = _Control(mode="delegated")
    payload = await realm_residue.realm_account_residue(control)
    assert payload == _unavailable("keycloak_provider_control_delegated")
    assert control.limits == []


@pytest.mark.asyncio
async def test_a_realm_that_is_not_the_runtime_issuer_is_not_reconciled(sso_settings, no_database):
    control = _Control(issuer="https://other.example.test/realms/akb")
    payload = await realm_residue.realm_account_residue(control)
    assert payload == _unavailable("realm_residue_authority_mismatch")
    assert control.limits == []


@pytest.mark.asyncio
async def test_an_available_read_projects_counts_and_value_minimal_items(sso_settings, monkeypatch):
    pending = _arrival(5)
    seen: list[tuple[str, tuple[str, ...]]] = []

    async def records(issuer: str, subjects: tuple[str, ...]):
        seen.append((issuer, subjects))
        return frozenset({"bound"}), {"pending": pending}

    monkeypatch.setattr(realm_residue, "read_akb_records", records)
    control = _Control(listing=RealmAccountListing(
        accounts=(_account("bound", 1), _account("pending", 2), _account("residue", None)),
        truncated=True,
    ))
    payload = await realm_residue.realm_account_residue(control)

    assert seen == [(_ISSUER, ("bound", "pending", "residue"))]
    assert payload == {
        "schema_version": 1,
        "state": "available",
        "unavailable_code": None,
        "scan_limit": realm_residue.REALM_ACCOUNT_SCAN_LIMIT,
        "truncated": True,
        "realm_account_count": 3,
        "bound_count": 1,
        "pending_arrival_count": 1,
        "residue_count": 1,
        "accounts": [
            {
                "realm_user_id": "residue",
                "username": "user-residue",
                "created_at": None,
                "arrival": None,
            },
            {
                "realm_user_id": "pending",
                "username": "user-pending",
                "created_at": "2026-10-02T09:00:00+00:00",
                "arrival": {
                    "admission_id": pending.admission_id,
                    "last_seen_at": "2026-10-05T09:00:00+00:00",
                },
            },
        ],
    }
    # The model declares exactly the projection, so nothing is filtered away.
    assert SsoRealmAccountResidueResponse.model_validate(payload).model_dump() == payload
    assert set(SsoRealmResidueAccountView.model_fields) == set(payload["accounts"][0])
    assert set(SsoRealmArrivalView.model_fields) == set(payload["accounts"][1]["arrival"])


def test_the_unavailable_projection_survives_the_model_unchanged():
    payload = _unavailable("realm_accounts_unavailable")
    assert SsoRealmAccountResidueResponse.model_validate(payload).model_dump() == payload


@pytest.mark.asyncio
async def test_an_empty_realm_is_zero_residue_because_it_was_measured(sso_settings, monkeypatch):
    async def records(_issuer, subjects):
        assert subjects == ()
        return frozenset(), {}

    monkeypatch.setattr(realm_residue, "read_akb_records", records)
    payload = await realm_residue.realm_account_residue(_Control())
    assert payload["state"] == "available"
    assert payload["residue_count"] == 0
    assert payload["accounts"] == []


# ── the Keycloak adapter: GET only, bounded, fail-closed ─────────────────────


def _config() -> KeycloakAdminConfig:
    return KeycloakAdminConfig(
        "https://internal.example.test", "https://sso.example.test", "akb",
        "manager", "fixture-secret", True,  # pragma: allowlist secret
    )


def _users(start: int, count: int) -> list[dict[str, object]]:
    return [
        {
            "id": f"user-{index:04d}",
            "username": f"name-{index:04d}",
            "createdTimestamp": 1_790_000_000_000 + index,
            "email": f"person{index}@example.com",
            "enabled": True,
            "emailVerified": True,
            "access": {"manage": False},
        }
        for index in range(start, start + count)
    ]


def _realm(total: int, calls: list[tuple[str, str, dict[str, str]]], *, override=None):
    def respond(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fixture-token"})
        assert request.url.path == "/admin/realms/akb/users"
        if override is not None:
            return override(request)
        first = int(request.url.params["first"])
        size = int(request.url.params["max"])
        return httpx.Response(200, json=_users(first, max(0, min(size, total - first))))

    return KeycloakProviderControl(_config(), transport=httpx.MockTransport(respond))


@pytest.mark.asyncio
async def test_the_listing_pages_through_the_realm_with_get_only():
    calls: list[tuple[str, str, dict[str, str]]] = []
    control = _realm(250, calls)
    listing = await control.list_realm_accounts(limit=1000)

    assert listing.truncated is False
    assert len(listing.accounts) == 250
    assert listing.accounts[0] == RealmAccount(
        user_id="user-0000",
        username="name-0000",
        created_at=datetime.fromtimestamp(1_790_000_000, tz=timezone.utc),
    )
    assert [method for method, _, _ in calls] == ["POST", "GET", "GET", "GET"]
    assert calls[0][1] == "/realms/akb/protocol/openid-connect/token"
    assert [params for _, _, params in calls[1:]] == [
        {"briefRepresentation": "true", "first": "0", "max": "100"},
        {"briefRepresentation": "true", "first": "100", "max": "100"},
        {"briefRepresentation": "true", "first": "200", "max": "100"},
    ]


@pytest.mark.asyncio
async def test_the_listing_states_truncation_instead_of_reading_without_bound():
    calls: list[tuple[str, str, dict[str, str]]] = []
    control = _realm(5000, calls)
    listing = await control.list_realm_accounts(limit=150)

    assert listing.truncated is True
    assert len(listing.accounts) == 150
    # One account past the bound proves truncation; no more is read.
    assert [params["max"] for _, _, params in calls[1:]] == ["100", "51"]


@pytest.mark.asyncio
async def test_a_realm_of_exactly_the_bound_is_not_truncated():
    calls: list[tuple[str, str, dict[str, str]]] = []
    listing = await _realm(150, calls).list_realm_accounts(limit=150)
    assert listing.truncated is False
    assert len(listing.accounts) == 150


@pytest.mark.asyncio
async def test_an_account_seen_twice_across_pages_is_counted_once():
    def shifted(request: httpx.Request) -> httpx.Response:
        first = int(request.url.params["first"])
        size = int(request.url.params["max"])
        # A concurrent insert shifts the second page back by one.
        start = first - 1 if first else 0
        return httpx.Response(200, json=_users(start, max(0, min(size, 120 - first))))

    listing = await _realm(0, [], override=shifted).list_realm_accounts(limit=1000)
    ids = [account.user_id for account in listing.accounts]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "body",
    [
        {"users": []},
        [{"username": "no-id", "createdTimestamp": 1}],
        [{"id": "", "username": "empty", "createdTimestamp": 1}],
        [{"id": "a\nb", "username": "control", "createdTimestamp": 1}],
        [{"id": "ok", "username": "", "createdTimestamp": 1}],
        [{"id": "ok", "username": "x" * 256, "createdTimestamp": 1}],
        [{"id": "ok", "username": "fine", "createdTimestamp": True}],
        [{"id": "ok", "username": "fine", "createdTimestamp": -1}],
        [{"id": "ok", "username": "fine", "createdTimestamp": "1"}],
    ],
)
@pytest.mark.asyncio
async def test_an_invalid_listing_fails_closed_without_a_partial_result(body):
    control = _realm(0, [], override=lambda _request: httpx.Response(200, json=body))
    with pytest.raises(ProviderControlError) as failure:
        await control.list_realm_accounts(limit=10)
    assert failure.value.code == "realm_accounts_invalid"


@pytest.mark.parametrize("status_code", [401, 403, 404, 500, 503])
@pytest.mark.asyncio
async def test_a_refused_listing_is_unavailable(status_code):
    control = _realm(0, [], override=lambda _request: httpx.Response(status_code))
    with pytest.raises(ProviderControlError) as failure:
        await control.list_realm_accounts(limit=10)
    assert failure.value.code == "realm_accounts_unavailable"


@pytest.mark.parametrize("token", ["invalid-\u2603", "has space", "line\nbreak", "quote\""])
@pytest.mark.asyncio
async def test_a_malformed_management_token_is_a_control_failure(token):
    """A token that cannot be a bearer value fails as ProviderControlError, so the
    projection reports it as unavailable instead of raising while the request is built."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": token})
        raise AssertionError("no listing request may be built with a malformed token")

    control = KeycloakProviderControl(_config(), transport=httpx.MockTransport(respond))
    with pytest.raises(ProviderControlError) as failure:
        await control.list_realm_accounts(limit=10)
    assert failure.value.code == "keycloak_provider_token_invalid"


@pytest.mark.asyncio
async def test_an_absent_creation_time_is_carried_as_unknown():
    body = [{"id": "federated", "username": "ldap-person"}]
    control = _realm(0, [], override=lambda _request: httpx.Response(200, json=body))
    listing = await control.list_realm_accounts(limit=10)
    assert listing.accounts == (RealmAccount(user_id="federated", username="ldap-person", created_at=None),)


@pytest.mark.parametrize("limit", [0, -1, realm_residue.REALM_ACCOUNT_SCAN_LIMIT * 10 + 1])
@pytest.mark.asyncio
async def test_the_listing_refuses_an_unbounded_or_empty_limit(limit):
    calls: list[tuple[str, str, dict[str, str]]] = []
    with pytest.raises(ProviderControlError):
        await _realm(10, calls).list_realm_accounts(limit=limit)
    assert calls == []


@pytest.mark.asyncio
async def test_a_delegated_adapter_refuses_before_any_request():
    calls: list[tuple[str, str, dict[str, str]]] = []
    config = KeycloakAdminConfig(
        "https://internal.example.test", "https://sso.example.test", "akb", "", "", True,
    )

    def respond(request):
        calls.append((request.method, request.url.path, {}))
        return httpx.Response(500)

    control = KeycloakProviderControl(config, transport=httpx.MockTransport(respond))
    with pytest.raises(ProviderControlError) as failure:
        await control.list_realm_accounts(limit=10)
    assert failure.value.code == "keycloak_provider_control_delegated"
    assert calls == []


# ── the route: SSO mode, product-admin read, no mutation ─────────────────────


def _admin() -> ProductAdminIdentity:
    return ProductAdminIdentity(
        user_id=uuid.uuid4(),
        external_identity_id=uuid.uuid4(),
        username="product-admin",
        email="admin@example.com",
        display_name="Product Admin",
        auth_method="keycloak",
    )


def _route_client(monkeypatch, payload, *, mode: str = "sso") -> TestClient:
    async def residue(_control):
        return payload

    monkeypatch.setattr(settings, "auth_mode", mode, raising=False)
    monkeypatch.setattr(admin_sso, "realm_account_residue", residue)
    monkeypatch.setattr(admin_sso, "get_keycloak_provider_control", lambda: object())
    app = FastAPI()
    app.include_router(admin_sso.router, prefix="/api/v1")
    app.dependency_overrides[admin_auth.get_current_product_admin] = _admin

    def refuse_mutation():
        raise AssertionError("a read must not resolve the mutation dependency")

    app.dependency_overrides[admin_auth.get_product_admin_mutation] = refuse_mutation
    return TestClient(app)


def test_the_route_returns_the_unavailable_state_as_a_value(monkeypatch):
    payload = _unavailable("realm_accounts_unavailable")
    response = _route_client(monkeypatch, payload).get("/api/v1/admin/sso/realm-account-residue")
    assert response.status_code == 200
    assert response.json() == payload


def test_the_route_exists_only_in_sso_mode(monkeypatch):
    response = _route_client(monkeypatch, _unavailable("x"), mode="local").get(
        "/api/v1/admin/sso/realm-account-residue"
    )
    assert response.status_code == 404


def _residue_route():
    from fastapi.routing import APIRoute

    routes = [
        route
        for route in admin_sso.router.routes
        if isinstance(route, APIRoute) and route.path == "/admin/sso/realm-account-residue"
    ]
    assert len(routes) == 1
    return routes[0]


def _dependency_calls(dependant) -> set[object]:
    calls = {dependant.call}
    for child in dependant.dependencies:
        calls |= _dependency_calls(child)
    return calls


def test_the_route_requires_a_product_admin_session_and_no_service_key():
    calls = _dependency_calls(_residue_route().dependant)
    assert admin_auth.get_current_product_admin in calls
    # Not the provider-control actor: that one also admits a platform service
    # key, and this read is for a signed-in product administrator.
    assert admin_sso.get_sso_provider_control_actor not in calls
    assert admin_auth.get_product_admin_mutation not in calls


def test_the_route_is_read_only():
    assert _residue_route().methods == {"GET"}


def test_the_shared_fixture_is_a_payload_this_model_accepts_unchanged():
    """The frontend parser tests read the same file, so both sides agree on it."""
    import json
    from pathlib import Path

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "admin_sso_realm_account_residue_v1.json").read_text()
    )
    assert SsoRealmAccountResidueResponse.model_validate(fixture).model_dump() == fixture
    assert fixture["realm_account_count"] == (
        fixture["bound_count"] + fixture["pending_arrival_count"] + fixture["residue_count"]
    )
    assert fixture["scan_limit"] == realm_residue.REALM_ACCOUNT_SCAN_LIMIT
