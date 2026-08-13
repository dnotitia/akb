"""Prove exact identity continuity through a real two-Keycloak broker chain."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import secrets
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
import uuid

import httpx
import jwt

from app.db import postgres
from app.services import auth_service, keycloak_oidc
from app.services.access_service import check_vault_access
from app.services.auth_verifier_profiles import verify_keycloak_access_v1
from app.sso.identity_migration import (
    apply_identity_migration,
    inspect_identity_migration,
    rollback_identity_migration,
)
from app.sso.keycloak_admin import (
    KeycloakAdminConfig,
    KeycloakProviderControl,
    ProviderControlError,
)
from app.sso.models import ProviderConfigureSpec


BROKER = "https://broker.localhost:19443"
BROKER_ISSUER = f"{BROKER}/realms/akb"
UPSTREAM = "https://upstream.localhost:19444"
UPSTREAM_ISSUER = f"{UPSTREAM}/realms/workforce"
API_AUDIENCE = f"{BROKER}/api"
_MANAGEMENT_ROLES = {
    "manage-identity-providers",
    "query-clients",
    "query-users",
    "view-clients",
    "view-realm",
    "view-users",
}


def _fail(code: str) -> RuntimeError:
    return RuntimeError(code)


def _require_object(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(code)
    return value


def _require_objects(value: object, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _fail(code)
    return value


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, object]] = []
        self._form: dict[str, object] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if tag == "form":
            self._form = {
                "action": values.get("action", ""),
                "method": (values.get("method") or "get").lower(),
                "inputs": {},
            }
        elif tag == "input" and self._form is not None:
            name = values.get("name")
            if name:
                inputs = self._form["inputs"]
                assert isinstance(inputs, dict)
                inputs[name] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None


def _login_form(response: httpx.Response) -> tuple[str, dict[str, str]]:
    parser = _FormParser()
    parser.feed(response.text)
    for form in parser.forms:
        inputs = form.get("inputs")
        if not isinstance(inputs, dict) or not {"username", "password"}.issubset(inputs):
            continue
        action = form.get("action")
        method = form.get("method")
        if not isinstance(action, str) or not action or method != "post":
            break
        return (
            urljoin(str(response.url), unescape(action)),
            {
                key: value
                for key, value in inputs.items()
                if isinstance(key, str) and isinstance(value, str)
            },
        )
    raise _fail("fixture_login_form_missing")


async def _authorization_code_tokens(
    *,
    issuer: str,
    client_id: str,
    redirect_uri: str,
    username: str,
    password: str,
    extra_authorize: dict[str, str] | None = None,
) -> dict[str, Any]:
    verifier, challenge = _pkce()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid profile email",
        "state": secrets.token_urlsafe(24),
        "nonce": secrets.token_urlsafe(24),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    params.update(extra_authorize or {})
    callback_code: str | None = None
    submitted = False
    async with httpx.AsyncClient(
        verify=False,
        follow_redirects=False,
        timeout=httpx.Timeout(20.0, connect=10.0),
    ) as client:
        response = await client.get(
            f"{issuer}/protocol/openid-connect/auth",
            params=params,
        )
        for _ in range(30):
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise _fail("fixture_authorization_redirect_invalid")
                target = urljoin(str(response.url), location)
                if target.startswith(redirect_uri):
                    query = parse_qs(urlsplit(target).query)
                    if "error" in query:
                        raise _fail("fixture_authorization_rejected")
                    values = query.get("code", [])
                    if len(values) != 1 or not values[0]:
                        raise _fail("fixture_authorization_code_missing")
                    callback_code = values[0]
                    break
                response = await client.get(target)
                continue
            if response.status_code == 200 and not submitted:
                action, data = _login_form(response)
                data.update(
                    {
                        "username": username,
                        "password": password,
                        "login": data.get("login") or "Sign In",
                    }
                )
                response = await client.post(action, data=data)
                submitted = True
                continue
            raise _fail("fixture_authorization_flow_failed")
        if callback_code is None:
            raise _fail("fixture_authorization_flow_exhausted")
        token_response = await client.post(
            f"{issuer}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code": callback_code,
                "code_verifier": verifier,
            },
        )
    if token_response.status_code != 200:
        raise _fail("fixture_authorization_code_exchange_failed")
    tokens = _require_object(token_response.json(), "fixture_token_response_invalid")
    if not isinstance(tokens.get("access_token"), str):
        raise _fail("fixture_access_token_missing")
    return tokens


async def _admin_token(client: httpx.AsyncClient, base_url: str) -> str:
    response = await client.post(
        f"{base_url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "fixture-admin",
            "password": "fixture-only-admin-password",  # pragma: allowlist secret
        },
    )
    if response.status_code != 200:
        raise _fail("fixture_admin_token_failed")
    value = _require_object(response.json(), "fixture_admin_token_invalid").get(
        "access_token"
    )
    if not isinstance(value, str) or not value:
        raise _fail("fixture_admin_token_invalid")
    return value


async def _exact_client(
    client: httpx.AsyncClient,
    *,
    admin_token: str,
    client_id: str,
) -> dict[str, Any]:
    response = await client.get(
        f"{BROKER}/admin/realms/akb/clients",
        params={"clientId": client_id},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    if response.status_code != 200:
        raise _fail("fixture_client_read_failed")
    matches = [
        item
        for item in _require_objects(response.json(), "fixture_client_read_failed")
        if item.get("clientId") == client_id
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
        raise _fail("fixture_client_not_exact")
    return matches[0]


async def _assert_management_roles(
    client: httpx.AsyncClient,
    *,
    admin_token: str,
) -> None:
    management = await _exact_client(
        client,
        admin_token=admin_token,
        client_id="akb-sso-manager",
    )
    realm_management = await _exact_client(
        client,
        admin_token=admin_token,
        client_id="realm-management",
    )
    management_uuid = management["id"]
    realm_management_uuid = realm_management["id"]
    service_response = await client.get(
        (
            f"{BROKER}/admin/realms/akb/clients/{management_uuid}"
            "/service-account-user"
        ),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    if service_response.status_code != 200:
        raise _fail("fixture_management_service_account_read_failed")
    service_user = _require_object(
        service_response.json(),
        "fixture_management_service_account_read_failed",
    )
    service_user_id = service_user.get("id")
    if not isinstance(service_user_id, str):
        raise _fail("fixture_management_service_account_read_failed")
    headers = {"Authorization": f"Bearer {admin_token}"}
    role_response = await client.get(
        (
            f"{BROKER}/admin/realms/akb/users/{service_user_id}"
            f"/role-mappings/clients/{realm_management_uuid}"
        ),
        headers=headers,
    )
    scope_response = await client.get(
        (
            f"{BROKER}/admin/realms/akb/clients/{management_uuid}"
            f"/scope-mappings/clients/{realm_management_uuid}"
        ),
        headers=headers,
    )
    if role_response.status_code != 200 or scope_response.status_code != 200:
        raise _fail("fixture_management_role_read_failed")
    role_names = {
        item.get("name")
        for item in _require_objects(
            role_response.json(),
            "fixture_management_role_read_failed",
        )
    }
    scope_names = {
        item.get("name")
        for item in _require_objects(
            scope_response.json(),
            "fixture_management_role_read_failed",
        )
    }
    if role_names != _MANAGEMENT_ROLES or scope_names != _MANAGEMENT_ROLES:
        raise _fail("fixture_management_roles_not_least_privilege")
    if "manage-users" in role_names or "manage-users" in scope_names:
        raise _fail("fixture_management_manage_users_present")


async def _operator_prelink() -> tuple[str, str]:
    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        upstream_admin = await _admin_token(client, UPSTREAM)
        upstream_response = await client.get(
            f"{UPSTREAM}/admin/realms/workforce/users",
            params={"username": "alice", "exact": "true"},
            headers={"Authorization": f"Bearer {upstream_admin}"},
        )
        if upstream_response.status_code != 200:
            raise _fail("fixture_upstream_user_read_failed")
        upstream_users = _require_objects(
            upstream_response.json(),
            "fixture_upstream_user_read_failed",
        )
        if len(upstream_users) != 1 or not isinstance(upstream_users[0].get("id"), str):
            raise _fail("fixture_upstream_user_not_exact")
        upstream_subject = upstream_users[0]["id"]

        broker_admin = await _admin_token(client, BROKER)
        create_response = await client.post(
            f"{BROKER}/admin/realms/akb/users",
            headers={"Authorization": f"Bearer {broker_admin}"},
            json={
                "username": "alice",
                "email": "alice@example.com",
                "emailVerified": True,
                "enabled": True,
                "firstName": "Alice",
                "lastName": "Fixture",
            },
        )
        if create_response.status_code != 201:
            raise _fail("fixture_broker_user_create_failed")
        location = create_response.headers.get("location")
        broker_subject = urlsplit(location or "").path.rsplit("/", 1)[-1]
        if not broker_subject:
            raise _fail("fixture_broker_user_location_invalid")
        link_response = await client.post(
            (
                f"{BROKER}/admin/realms/akb/users/{broker_subject}"
                "/federated-identity/workforce"
            ),
            headers={"Authorization": f"Bearer {broker_admin}"},
            json={
                "identityProvider": "workforce",
                "userId": upstream_subject,
                "userName": "alice",
            },
        )
        if link_response.status_code != 204:
            raise _fail("fixture_prelink_create_failed")

        await _assert_management_roles(client, admin_token=broker_admin)
    return upstream_subject, broker_subject


async def _operator_remove_prelink(broker_subject: str) -> None:
    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        broker_admin = await _admin_token(client, BROKER)
        headers = {"Authorization": f"Bearer {broker_admin}"}
        unlink = await client.delete(
            (
                f"{BROKER}/admin/realms/akb/users/{broker_subject}"
                "/federated-identity/workforce"
            ),
            headers=headers,
        )
        if unlink.status_code != 204:
            raise _fail("fixture_prelink_remove_failed")
        delete_user = await client.delete(
            f"{BROKER}/admin/realms/akb/users/{broker_subject}",
            headers=headers,
        )
        if delete_user.status_code != 204:
            raise _fail("fixture_broker_user_remove_failed")


async def _seed_akb(upstream_subject: str) -> dict[str, object]:
    await postgres.init_db()
    pool = await postgres.get_pool()
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    token_id = uuid.uuid4()
    owned_vault_id = uuid.uuid4()
    shared_vault_id = uuid.uuid4()
    raw_pat, token_hash, token_prefix = auth_service.generate_pat()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO users (
                    id, username, email, password_hash, display_name, is_admin,
                    auth_provider, account_status, account_kind
                ) VALUES
                    ($1, 'alice', 'alice@example.com', $2, 'Alice', false,
                     'keycloak', 'active', 'human'),
                    ($3, 'owner', 'owner@example.com', $4, 'Fixture Owner', false,
                     'keycloak', 'active', 'human')
                """,
                user_id,
                "!keycloak-sso:no-local-login!",  # pragma: allowlist secret
                other_user_id,
                "!keycloak-sso:no-local-login!",  # pragma: allowlist secret
            )
            await conn.execute(
                """
                INSERT INTO external_identities (
                    user_id, issuer, subject, username_snapshot, email_snapshot
                ) VALUES ($1, $2, $3, 'alice', 'alice@example.com')
                """,
                user_id,
                UPSTREAM_ISSUER,
                upstream_subject,
            )
            await conn.execute(
                """
                INSERT INTO tokens (
                    id, user_id, name, token_hash, token_prefix, scopes, key_class
                ) VALUES ($1, $2, 'continuity-pat', $3, $4,
                          ARRAY['read', 'write'], 'pat')
                """,
                token_id,
                user_id,
                token_hash,
                token_prefix,
            )
            await conn.execute(
                """
                INSERT INTO vaults (id, name, git_path, owner_id)
                VALUES
                    ($1, 'continuity-owned', '/tmp/continuity-owned.git', $2),
                    ($3, 'continuity-shared', '/tmp/continuity-shared.git', $4)
                """,
                owned_vault_id,
                user_id,
                shared_vault_id,
                other_user_id,
            )
            await conn.execute(
                """
                INSERT INTO vault_access (vault_id, user_id, role, granted_by)
                VALUES ($1, $2, 'writer', $3)
                """,
                shared_vault_id,
                user_id,
                other_user_id,
            )
    return {
        "pool": pool,
        "user_id": user_id,
        "token_id": token_id,
        "raw_pat": raw_pat,
        "owned_vault_id": owned_vault_id,
        "shared_vault_id": shared_vault_id,
    }


async def _assert_continuity(
    state: dict[str, object],
    *,
    upstream_subject: str,
    broker_subject: str,
    expect_broker_binding: bool,
) -> None:
    pool = state["pool"]
    user_id = state["user_id"]
    token_id = state["token_id"]
    assert hasattr(pool, "acquire")
    async with pool.acquire() as conn:  # type: ignore[union-attr]
        user = await conn.fetchrow(
            "SELECT id, username, account_status, account_kind FROM users WHERE id = $1",
            user_id,
        )
        bindings = await conn.fetch(
            """
            SELECT issuer, subject, user_id FROM external_identities
             WHERE user_id = $1 ORDER BY issuer
            """,
            user_id,
        )
        token_owner = await conn.fetchval(
            "SELECT user_id FROM tokens WHERE id = $1",
            token_id,
        )
        owned = await conn.fetchval(
            "SELECT owner_id FROM vaults WHERE id = $1",
            state["owned_vault_id"],
        )
        role = await conn.fetchval(
            "SELECT role FROM vault_access WHERE vault_id = $1 AND user_id = $2",
            state["shared_vault_id"],
            user_id,
        )
    if user is None or user["id"] != user_id:
        raise _fail("fixture_user_continuity_failed")
    if user["account_status"] != "active" or user["account_kind"] != "human":
        raise _fail("fixture_user_governance_changed")
    expected = [(UPSTREAM_ISSUER, upstream_subject)]
    if expect_broker_binding:
        expected.insert(0, (BROKER_ISSUER, broker_subject))
    actual = [(row["issuer"], row["subject"]) for row in bindings]
    if actual != expected or any(row["user_id"] != user_id for row in bindings):
        raise _fail("fixture_external_identity_continuity_failed")
    if token_owner != user_id or owned != user_id or role != "writer":
        raise _fail("fixture_authorization_continuity_failed")

    pat_user = await auth_service.resolve_rest_user_authorization(
        f"Bearer {state['raw_pat']}"
    )
    if pat_user is None or pat_user.user_id != str(user_id):
        raise _fail("fixture_pat_continuity_failed")
    owned_access = await check_vault_access(
        str(user_id),
        "continuity-owned",
        "owner",
    )
    shared_access = await check_vault_access(
        str(user_id),
        "continuity-shared",
        "writer",
    )
    if owned_access["role"] != "owner" or shared_access["role"] != "writer":
        raise _fail("fixture_vault_access_continuity_failed")


async def main() -> None:
    control = KeycloakProviderControl(
        KeycloakAdminConfig(
            internal_base_url=BROKER,
            public_base_url=BROKER,
            realm="akb",
            management_client_id="akb-sso-manager",
            management_client_secret="fixture-only-management-secret",  # pragma: allowlist secret
            verify_ssl=False,
        )
    )
    configured_mutation = await control.configure(
        ProviderConfigureSpec(
            provider_type="keycloak-oidc",
            alias="workforce",
            display_name="Company SSO",
            issuer=UPSTREAM_ISSUER,
            discovery_url=f"{UPSTREAM_ISSUER}/.well-known/openid-configuration",
            client_id="akb-broker",
            client_secret="fixture-only-upstream-client-secret",  # pragma: allowlist secret
        )
    )
    configured = configured_mutation.after
    if (
        configured_mutation.before is not None
        or configured.state != "configured_disabled"
        or not configured.supports_identity_migration
    ):
        raise _fail("fixture_configure_readback_failed")
    preserved_mutation = await control.configure(
        ProviderConfigureSpec(
            provider_type="keycloak-oidc",
            alias="workforce",
            display_name="Company SSO",
            issuer=UPSTREAM_ISSUER,
            discovery_url=f"{UPSTREAM_ISSUER}/.well-known/openid-configuration",
            client_id="akb-broker",
            client_secret=None,
        )
    )
    if (
        preserved_mutation.before is None
        or not preserved_mutation.before.client_secret_configured
        or not preserved_mutation.after.client_secret_configured
    ):
        raise _fail("fixture_secret_preservation_failed")

    upstream_subject, broker_subject = await _operator_prelink()
    prelink = await control.verify_identity_prelink(
        "workforce",
        broker_subject=broker_subject,
        upstream_subject=upstream_subject,
    )
    if (
        prelink.provider_state != "configured_disabled"
        or prelink.upstream_issuer != UPSTREAM_ISSUER
        or prelink.broker_issuer != BROKER_ISSUER
    ):
        raise _fail("fixture_prelink_readback_failed")

    state = await _seed_akb(upstream_subject)
    migration = await inspect_identity_migration(
        existing_user_id=str(state["user_id"]),
        old_issuer=UPSTREAM_ISSUER,
        old_subject=upstream_subject,
        new_issuer=BROKER_ISSUER,
        new_subject=broker_subject,
    )
    if migration.state != "ready_to_link":
        raise _fail("fixture_migration_preflight_failed")
    linked = await apply_identity_migration(
        existing_user_id=str(state["user_id"]),
        old_issuer=UPSTREAM_ISSUER,
        old_subject=upstream_subject,
        new_issuer=BROKER_ISSUER,
        new_subject=broker_subject,
        actor_id="fixture-product-admin",
    )
    if linked.state != "linked":
        raise _fail("fixture_migration_apply_failed")
    await _assert_continuity(
        state,
        upstream_subject=upstream_subject,
        broker_subject=broker_subject,
        expect_broker_binding=True,
    )

    enabled = (await control.set_enabled("workforce", enabled=True)).after
    catalog = await control.list_providers(force_refresh=True)
    if enabled.state != "enabled" or [item.alias for item in catalog] != ["workforce"]:
        raise _fail("fixture_enable_readback_failed")

    broker_tokens = await _authorization_code_tokens(
        issuer=BROKER_ISSUER,
        client_id="fixture-browser",
        redirect_uri="https://client.localhost/callback",
        username="alice",
        password="fixture-only-alice-password",  # pragma: allowlist secret
        extra_authorize={"kc_idp_hint": "workforce"},
    )
    broker_access_token = broker_tokens["access_token"]
    keycloak_oidc._service = None  # noqa: SLF001 - disposable process fixture
    principal = await verify_keycloak_access_v1(broker_access_token, "api")
    if principal is None:
        raise _fail("fixture_broker_token_profile_verification_failed")
    if principal.subject != broker_subject:
        raise _fail("fixture_broker_token_subject_not_prelinked_user")
    broker_user = await auth_service.project_verified_principal(principal)
    if broker_user is None or broker_user.user_id != str(state["user_id"]):
        raise _fail("fixture_broker_token_projection_failed")
    await _assert_continuity(
        state,
        upstream_subject=upstream_subject,
        broker_subject=broker_subject,
        expect_broker_binding=True,
    )

    upstream_tokens = await _authorization_code_tokens(
        issuer=UPSTREAM_ISSUER,
        client_id="upstream-probe",
        redirect_uri="https://upstream-client.localhost/callback",
        username="alice",
        password="fixture-only-alice-password",  # pragma: allowlist secret
    )
    upstream_access_token = upstream_tokens["access_token"]
    upstream_claims = jwt.decode(
        upstream_access_token,
        options={"verify_signature": False},
    )
    raw_audience = upstream_claims.get("aud")
    audiences = {raw_audience} if isinstance(raw_audience, str) else set(raw_audience or [])
    if upstream_claims.get("iss") != UPSTREAM_ISSUER or API_AUDIENCE not in audiences:
        raise _fail("fixture_upstream_collision_probe_invalid")
    if (
        await auth_service.resolve_keycloak_access_token(
            upstream_access_token,
            route_profile="api",
        )
        is not None
    ):
        raise _fail("fixture_upstream_token_accepted")

    disabled = (await control.set_enabled("workforce", enabled=False)).after
    if disabled.state != "configured_disabled":
        raise _fail("fixture_disable_readback_failed")
    rolled_back = await rollback_identity_migration(
        existing_user_id=str(state["user_id"]),
        old_issuer=UPSTREAM_ISSUER,
        old_subject=upstream_subject,
        new_issuer=BROKER_ISSUER,
        new_subject=broker_subject,
        actor_id="fixture-product-admin",
    )
    if rolled_back.state != "ready_to_link":
        raise _fail("fixture_migration_rollback_failed")
    await _assert_continuity(
        state,
        upstream_subject=upstream_subject,
        broker_subject=broker_subject,
        expect_broker_binding=False,
    )
    await _operator_remove_prelink(broker_subject)
    try:
        await control.verify_identity_prelink(
            "workforce",
            broker_subject=broker_subject,
            upstream_subject=upstream_subject,
        )
    except ProviderControlError as error:
        if error.code != "identity_prelink_user_not_found":
            raise
    else:
        raise _fail("fixture_operator_cleanup_not_observed")

    service = keycloak_oidc._service  # noqa: SLF001 - disposable process fixture
    if service is not None:
        await service.aclose()
        keycloak_oidc._service = None  # noqa: SLF001
    await postgres.close_pool()
    print(
        json.dumps(
            {
                "schema_version": 2,
                "provider_type": enabled.provider_type,
                "alias": enabled.alias,
                "configure_state": configured.state,
                "enabled_state": enabled.state,
                "disabled_state": disabled.state,
                "management_roles": "least-privilege-readback-verified",
                "prelink": "exact-readback-verified",
                "broker_login": "authorization-code-pkce-verified",
                "akb_user_continuity": "verified",
                "pat_continuity": "verified",
                "vault_continuity": "owner-and-writer-verified",
                "upstream_token_rejected": True,
                "rollback": "binding-and-operator-cleanup-verified",
                "client_secret_exposed": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
