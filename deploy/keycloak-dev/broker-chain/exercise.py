"""Prove exact identity continuity through a real two-Keycloak broker chain."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
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
                "buttons": [],
            }
        elif tag == "input" and self._form is not None:
            name = values.get("name")
            if name:
                inputs = self._form["inputs"]
                assert isinstance(inputs, dict)
                inputs[name] = values.get("value") or ""
        elif tag == "button" and self._form is not None:
            # The confirm-link page carries no inputs at all -- its two choices
            # are submit buttons -- so a parser that reads only inputs sees an
            # empty form and cannot tell that page from a redirect.
            name = values.get("name")
            if name:
                buttons = self._form["buttons"]
                assert isinstance(buttons, list)
                buttons.append(name)

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


PRODUCT_ADMIN = "admin"
PRODUCT_ADMIN_EMAIL = "admin@broker.localhost"
INSTALLED_CREDENTIAL = "fixture-only-installed-admin-password"  # pragma: allowlist secret


def _rotation_authority() -> tuple[str, str]:
    """The transient authority the runner created, or a refusal."""
    client_id = os.environ.get("AKB_FIXTURE_ROTATION_CLIENT_ID", "")
    client_secret = os.environ.get("AKB_FIXTURE_ROTATION_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise _fail("fixture_rotation_authority_absent")
    return client_id, client_secret


async def _client_credentials_token(
    client: httpx.AsyncClient,
    *,
    realm: str,
    client_id: str,
    client_secret: str,
) -> str | None:
    response = await client.post(
        f"{BROKER}/realms/{realm}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if response.status_code in {400, 401, 403, 404}:
        return None
    if response.status_code != 200:
        raise _fail("fixture_client_credentials_failed")
    token = _require_object(
        response.json(),
        "fixture_client_credentials_invalid",
    ).get("access_token")
    if not isinstance(token, str) or not token:
        raise _fail("fixture_client_credentials_invalid")
    return token


async def _exact_realm_user(
    client: httpx.AsyncClient,
    *,
    token: str,
    username: str,
) -> dict[str, Any]:
    response = await client.get(
        f"{BROKER}/admin/realms/akb/users",
        params={"username": username, "exact": "true"},
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code != 200:
        raise _fail("fixture_product_admin_read_failed")
    matches = [
        item
        for item in _require_objects(response.json(), "fixture_product_admin_read_failed")
        if item.get("username") == username
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
        raise _fail("fixture_product_admin_not_exact")
    return matches[0]


async def _login_outcome(password: str) -> str:
    """Drive a real browser login and say what the realm did with it.

    Three outcomes are distinguishable without reading a token: the credential
    is refused and the login form comes back, the credential is accepted and
    Keycloak demands the forced password change, or the credential is accepted
    outright. The middle one is what a correctly delivered administrator
    credential must produce -- it is one use, and Keycloak says so before it
    lets anyone in.
    """
    params = {
        "client_id": "fixture-browser",
        "redirect_uri": "https://client.localhost/callback",
        "response_type": "code",
        "scope": "openid profile email",
        "state": secrets.token_urlsafe(24),
        "nonce": secrets.token_urlsafe(24),
    }
    verifier, challenge = _pkce()
    params["code_challenge"] = challenge
    params["code_challenge_method"] = "S256"
    del verifier
    async with httpx.AsyncClient(
        verify=False,
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=10.0),
    ) as client:
        page = await client.get(
            f"{BROKER_ISSUER}/protocol/openid-connect/auth",
            params=params,
        )
        if page.status_code != 200:
            raise _fail("fixture_login_page_unavailable")
        action, data = _login_form(page)
        data.update(
            {
                "username": PRODUCT_ADMIN,
                "password": password,
                "login": data.get("login") or "Sign In",
            }
        )
        result = await client.post(action, data=data)
        if result.status_code != 200:
            raise _fail("fixture_login_post_failed")
        parser = _FormParser()
        parser.feed(result.text)
        fields: set[str] = set()
        for form in parser.forms:
            inputs = form.get("inputs")
            if isinstance(inputs, dict):
                fields.update(inputs)
    if {"password-new", "password-confirm"} & fields:
        return "accepted_forced_change"
    if {"username", "password"} & fields:
        return "refused"
    return "accepted"


async def _prove_transient_authority_boundary() -> dict[str, object]:
    """Prove where the authority to mint an administrator credential lives.

    Three facts, and each one is only worth having with the other two:

    1. the permanent management account CANNOT reset the product
       administrator's password. Its six realm-management roles are asserted
       elsewhere in this fixture; this is the consequence of them, measured
       against a real realm rather than inferred from a role list;
    2. an authority created for the purpose CAN, and the credential it installs
       is one use -- the realm demands a replacement at first login;
    3. once that authority is retired, neither the token it was holding nor a
       newly requested one is accepted. The client is gone and cannot come back.

    Fact 2 alone would read as a hole in the boundary. Facts 1 and 3 are what
    make it a door: it exists only while someone is holding it open.
    """
    client_id, client_secret = _rotation_authority()
    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        broker_admin = await _admin_token(client, BROKER)
        create = await client.post(
            f"{BROKER}/admin/realms/akb/users",
            headers={"Authorization": f"Bearer {broker_admin}"},
            json={
                "username": PRODUCT_ADMIN,
                "email": PRODUCT_ADMIN_EMAIL,
                "emailVerified": True,
                "enabled": True,
                "firstName": "AKB",
                "lastName": "Product Administrator",
                "requiredActions": ["UPDATE_PASSWORD"],
                "credentials": [
                    {
                        "type": "password",
                        "value": INSTALLED_CREDENTIAL,
                        "temporary": True,
                    }
                ],
            },
        )
        if create.status_code != 201:
            raise _fail("fixture_product_admin_create_failed")

        management_token = await _client_credentials_token(
            client,
            realm="akb",
            client_id="akb-sso-manager",
            client_secret="fixture-only-management-secret",  # pragma: allowlist secret
        )
        if management_token is None:
            raise _fail("fixture_management_token_failed")
        product_admin = await _exact_realm_user(
            client,
            token=management_token,
            username=PRODUCT_ADMIN,
        )
        product_admin_id = product_admin["id"]
        # The permanent account can SEE the administrator -- query-users and
        # view-users are in its six -- and must not be able to replace its
        # credential.
        standing_reset = await client.put(
            f"{BROKER}/admin/realms/akb/users/{product_admin_id}/reset-password",
            headers={"Authorization": f"Bearer {management_token}"},
            json={"type": "password", "value": "would-be-a-standing-mint", "temporary": False},
        )
        if standing_reset.status_code != 403:
            raise _fail("fixture_standing_client_can_mint")

        authority_token = await _client_credentials_token(
            client,
            realm="master",
            client_id=client_id,
            client_secret=client_secret,
        )
        if authority_token is None:
            raise _fail("fixture_rotation_authority_unusable")
        rotated_credential = "fixture-only-rotated-admin-password"  # pragma: allowlist secret
        minted = await client.put(
            f"{BROKER}/admin/realms/akb/users/{product_admin_id}/reset-password",
            headers={"Authorization": f"Bearer {authority_token}"},
            json={"type": "password", "value": rotated_credential, "temporary": True},
        )
        if minted.status_code != 204:
            raise _fail("fixture_transient_authority_mint_failed")
        after = await _exact_realm_user(
            client,
            token=management_token,
            username=PRODUCT_ADMIN,
        )
        if after.get("id") != product_admin_id:
            raise _fail("fixture_product_admin_changed_under_rotation")
        if after.get("requiredActions") != ["UPDATE_PASSWORD"]:
            raise _fail("fixture_rotated_credential_is_not_one_use")
        if after.get("federationLink"):
            raise _fail("fixture_product_admin_is_federated")

        authority_clients = await client.get(
            f"{BROKER}/admin/realms/master/clients",
            params={"clientId": client_id},
            headers={"Authorization": f"Bearer {authority_token}"},
        )
        if authority_clients.status_code != 200:
            raise _fail("fixture_rotation_authority_read_failed")
        matches = [
            item
            for item in _require_objects(
                authority_clients.json(),
                "fixture_rotation_authority_read_failed",
            )
            if item.get("clientId") == client_id
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
            raise _fail("fixture_rotation_authority_not_exact")
        retired = await client.delete(
            f"{BROKER}/admin/realms/master/clients/{matches[0]['id']}",
            headers={"Authorization": f"Bearer {authority_token}"},
        )
        if retired.status_code != 204:
            raise _fail("fixture_rotation_authority_retire_failed")

        # Both directions, because either alone can be true of a live client: a
        # revoked session would fail the first, and a cached token would pass
        # the second.
        proven = False
        for _attempt in range(5):
            prior = await client.get(
                f"{BROKER}/admin/realms/master",
                headers={"Authorization": f"Bearer {authority_token}"},
            )
            fresh = await _client_credentials_token(
                client,
                realm="master",
                client_id=client_id,
                client_secret=client_secret,
            )
            if prior.status_code in {401, 403} and fresh is None:
                proven = True
                break
            await asyncio.sleep(0.5)
        if not proven:
            raise _fail("fixture_rotation_authority_still_active")

    # The credential is proven at the door the owner actually uses, not by
    # reading it back from the store that was just written.
    if await _login_outcome(INSTALLED_CREDENTIAL) != "refused":
        raise _fail("fixture_replaced_credential_still_works")
    if await _login_outcome(rotated_credential) != "accepted_forced_change":
        raise _fail("fixture_rotated_credential_rejected")

    return {
        "standing_client_mint": "refused-403",
        "transient_authority_mint": "accepted-and-forced-change",
        "replaced_credential": "refused-at-login",
        "authority_retirement": "old-and-new-token-rejected",
    }


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


# The account member invitation seeds into a workspace's own realm, reproduced
# exactly. `_operator_prelink` above is deliberately NOT this shape: it marks
# the address verified and writes an explicit federated identity link, because
# it is measuring identity continuity for someone an operator already linked by
# hand. The invitation ceremony creates neither, and a case that keeps either
# property proves nothing about invitation -- it proves only that a linked
# account logs in, which was never in doubt.
INVITED_EMAIL = "invitee@example.com"
INVITED_UPSTREAM_USERNAME = "invitee"
INVITED_UPSTREAM_PASSWORD = "fixture-only-invitee-password"  # pragma: allowlist secret


def _seeded_account_payload() -> dict[str, Any]:
    """The invited person's realm account, field for field.

    Username and email are the same value because Keycloak's first-broker-login
    detects a collision with an existing account on either key. No credential
    and no required action, so nothing can sign into it directly. Not marked
    verified, because verification is a statement about what THIS realm has
    proved and this realm has proved nothing about this address.
    """
    return {
        "username": INVITED_EMAIL,
        "email": INVITED_EMAIL,
        "enabled": True,
        "emailVerified": False,
    }


def _evidence(code: str, evidence: object) -> RuntimeError:
    """A failure that says what was on screen, not just that something failed.

    The codes in this fixture are normally enough because the step that raises
    them is the whole story. This phase is measuring an unproven claim about
    another system's behaviour, so a bare code would report that the claim is
    false without reporting what Keycloak actually did instead.
    """
    return RuntimeError(f"{code} {json.dumps(evidence, sort_keys=True)}")


def _page_shape(response: httpx.Response) -> dict[str, object]:
    """Name the page a browser is being shown, in fields rather than prose.

    The rendered text is theme markup and inline script; the form fields are
    the meaning. A username/password pair on the upstream host is the invited
    person's own credential. The same pair on the broker host is Keycloak
    demanding that they re-authenticate as the account being linked to -- which
    for a seeded account means proving a credential that was never created. A
    `submitAction` button is Keycloak asking which account this login belongs
    to.

    Nothing recorded here includes the action URL: it carries a single-use
    session code, and the receipt is secret-free.
    """
    parser = _FormParser()
    parser.feed(response.text)
    split = urlsplit(str(response.url))
    fields: set[str] = set()
    buttons: set[str] = set()
    for form in parser.forms:
        inputs = form.get("inputs")
        if isinstance(inputs, dict):
            fields.update(key for key in inputs if isinstance(key, str))
        names = form.get("buttons")
        if isinstance(names, list):
            buttons.update(name for name in names if isinstance(name, str))
    if "submitAction" in buttons:
        kind = "confirm-link"
    elif {"username", "password"} <= fields:
        kind = (
            "upstream-credential-form"
            if split.netloc == urlsplit(UPSTREAM).netloc
            else "existing-account-reauthentication"
        )
    elif {"password-new", "password-confirm"} & fields:
        kind = "forced-credential-change"
    elif {"firstName", "lastName", "email"} & fields:
        kind = "review-profile"
    else:
        kind = "other"
    return {
        "kind": kind,
        "host": split.netloc,
        "path": split.path,
        "fields": sorted(fields),
        "buttons": sorted(buttons),
    }


async def _invited_broker_login() -> dict[str, object]:
    """Drive the invited person's login, supplying only what they have.

    An invited person holds exactly one thing: their credential in the
    workspace's upstream directory. So this supplies exactly that, exactly
    once, and stops at the next page that asks for anything else -- because
    "the first accepted login sets the flag" is a claim that there is no next
    page. Whatever it stops at is the evidence.
    """
    verifier, challenge = _pkce()
    redirect_uri = "https://client.localhost/callback"
    params = {
        "client_id": "fixture-browser",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid profile email",
        "state": secrets.token_urlsafe(24),
        "nonce": secrets.token_urlsafe(24),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "kc_idp_hint": "workforce",
    }
    pages: list[dict[str, object]] = []
    code: str | None = None
    supplied_credential = False
    async with httpx.AsyncClient(
        verify=False,
        follow_redirects=False,
        timeout=httpx.Timeout(20.0, connect=10.0),
    ) as client:
        response = await client.get(
            f"{BROKER_ISSUER}/protocol/openid-connect/auth",
            params=params,
        )
        for _ in range(30):
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise _fail("fixture_invited_authorization_redirect_invalid")
                target = urljoin(str(response.url), location)
                if target.startswith(redirect_uri):
                    query = parse_qs(urlsplit(target).query)
                    if "error" in query:
                        return {"pages": pages, "code": None, "rejected": True}
                    values = query.get("code", [])
                    if len(values) != 1 or not values[0]:
                        raise _fail("fixture_invited_authorization_code_missing")
                    code = values[0]
                    break
                response = await client.get(target)
                continue
            if response.status_code != 200:
                raise _fail("fixture_invited_authorization_flow_failed")
            page = _page_shape(response)
            pages.append(page)
            if page["kind"] != "upstream-credential-form" or supplied_credential:
                break
            action, data = _login_form(response)
            data.update(
                {
                    "username": INVITED_UPSTREAM_USERNAME,
                    "password": INVITED_UPSTREAM_PASSWORD,
                    "login": data.get("login") or "Sign In",
                }
            )
            response = await client.post(action, data=data)
            supplied_credential = True
        if code is None:
            return {"pages": pages, "code": None}
        token_response = await client.post(
            f"{BROKER_ISSUER}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "fixture-browser",
                "redirect_uri": redirect_uri,
                "code": code,
                "code_verifier": verifier,
            },
        )
    if token_response.status_code != 200:
        raise _fail("fixture_invited_authorization_code_exchange_failed")
    tokens = _require_object(token_response.json(), "fixture_invited_token_invalid")
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str):
        raise _fail("fixture_invited_token_invalid")
    return {"pages": pages, "code": code, "access_token": access_token}


async def _invited_accounts(
    client: httpx.AsyncClient,
    *,
    admin_token: str,
) -> list[dict[str, Any]]:
    """Every broker-realm account holding the invited address, with its shape.

    Read by address rather than by id on purpose. The id is what the invitation
    binds AKB to, so asking Keycloak "which accounts answer to this address"
    is the only question that can reveal a second one.
    """
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.get(
        f"{BROKER}/admin/realms/akb/users",
        params={"email": INVITED_EMAIL, "exact": "true"},
        headers=headers,
    )
    if response.status_code != 200:
        raise _fail("fixture_invited_account_read_failed")
    accounts: list[dict[str, Any]] = []
    for user in _require_objects(response.json(), "fixture_invited_account_read_failed"):
        subject = user.get("id")
        if not isinstance(subject, str):
            raise _fail("fixture_invited_account_read_failed")
        credentials = await client.get(
            f"{BROKER}/admin/realms/akb/users/{subject}/credentials",
            headers=headers,
        )
        links = await client.get(
            f"{BROKER}/admin/realms/akb/users/{subject}/federated-identity",
            headers=headers,
        )
        if credentials.status_code != 200 or links.status_code != 200:
            raise _fail("fixture_invited_account_read_failed")
        accounts.append(
            {
                "subject": subject,
                "email_verified": user.get("emailVerified"),
                "enabled": user.get("enabled"),
                "required_actions": user.get("requiredActions") or [],
                "federation_link": user.get("federationLink"),
                "credential_types": sorted(
                    str(item.get("type"))
                    for item in _require_objects(
                        credentials.json(),
                        "fixture_invited_account_read_failed",
                    )
                ),
                "federated_identities": sorted(
                    str(item.get("identityProvider"))
                    for item in _require_objects(
                        links.json(),
                        "fixture_invited_account_read_failed",
                    )
                ),
            }
        )
    return accounts


async def _prove_invitation_seeded_account_links() -> dict[str, object]:
    """Does the account invitation seeds actually receive the invited person?

    Everything else about member invitation is settled by construction: the
    ceremony creates the account, reads its id back, and binds AKB to that id.
    What is not settled is the step after it, which belongs to Keycloak. The
    invitation deliberately leaves the seeded account with no credential, no
    required action, an unverified address, and no federated identity link, on
    the stated ground that the first accepted login through the broker will
    find it and set the flag from the upstream's signed proof.

    That is a claim about first-broker-login. This phase supplies the invited
    person's upstream credential and nothing else, then asks Keycloak three
    questions whose answers are independent:

      * which account did the login land on -- the seeded one, or a second one
        Keycloak created, which would leave the AKB binding naming a row nobody
        ever signs in as;
      * what became of `emailVerified` on it;
      * what, if anything, was demanded beyond that credential.

    An account that eventually links after a step the invited person cannot
    complete is a failure, not a slow success, so the driver stops at the first
    such step rather than answering it.
    """
    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        upstream_admin = await _admin_token(client, UPSTREAM)
        created_upstream = await client.post(
            f"{UPSTREAM}/admin/realms/workforce/users",
            headers={"Authorization": f"Bearer {upstream_admin}"},
            json={
                "username": INVITED_UPSTREAM_USERNAME,
                "email": INVITED_EMAIL,
                "emailVerified": True,
                "enabled": True,
                "firstName": "Invited",
                "lastName": "Person",
                "credentials": [
                    {
                        "type": "password",
                        "value": INVITED_UPSTREAM_PASSWORD,
                        "temporary": False,
                    }
                ],
            },
        )
        if created_upstream.status_code != 201:
            raise _fail("fixture_invited_upstream_create_failed")
        upstream_subject = urlsplit(
            created_upstream.headers.get("location") or ""
        ).path.rsplit("/", 1)[-1]
        if not upstream_subject:
            raise _fail("fixture_invited_upstream_location_invalid")

        broker_admin = await _admin_token(client, BROKER)
        seeded = await client.post(
            f"{BROKER}/admin/realms/akb/users",
            headers={"Authorization": f"Bearer {broker_admin}"},
            json=_seeded_account_payload(),
        )
        if seeded.status_code != 201:
            raise _fail("fixture_invited_seed_failed")
        seeded_subject = urlsplit(seeded.headers.get("location") or "").path.rsplit(
            "/", 1
        )[-1]
        if not seeded_subject:
            raise _fail("fixture_invited_seed_location_invalid")

        # The shape is the precondition, and it is checked rather than assumed.
        # Every one of these properties is a way for a later pass to be true
        # for a reason that has nothing to do with invitation: a credential
        # would let the person answer a re-authentication demand, and a
        # federated identity link would skip first-broker-login altogether.
        before = await _invited_accounts(client, admin_token=broker_admin)
        if [item["subject"] for item in before] != [seeded_subject]:
            raise _evidence("fixture_seeded_account_not_exact", before)
        seeded_shape = before[0]
        if seeded_shape["enabled"] is not True:
            raise _evidence("fixture_seeded_account_not_enabled", seeded_shape)
        if seeded_shape["email_verified"] is not False:
            raise _evidence("fixture_seeded_account_email_verified", seeded_shape)
        if seeded_shape["credential_types"]:
            raise _evidence("fixture_seeded_account_carries_credential", seeded_shape)
        if seeded_shape["required_actions"]:
            raise _evidence("fixture_seeded_account_has_required_action", seeded_shape)
        if seeded_shape["federated_identities"] or seeded_shape["federation_link"]:
            raise _evidence("fixture_seeded_account_is_prelinked", seeded_shape)

    login = await _invited_broker_login()
    pages = login["pages"]
    assert isinstance(pages, list)
    demanded = [page for page in pages if page["kind"] != "upstream-credential-form"]
    if demanded or len(pages) != 1:
        raise _evidence(
            "fixture_invited_login_needs_more_than_the_upstream_credential",
            pages,
        )
    access_token = login.get("access_token")
    if not isinstance(access_token, str):
        raise _evidence("fixture_invited_login_produced_no_token", pages)

    # The subject is read out of a token this runtime's own verifier accepted,
    # not out of an admin read, because the binding written at invitation is
    # matched against exactly this claim.
    principal = await verify_keycloak_access_v1(access_token, "api")
    if principal is None:
        raise _fail("fixture_invited_token_verification_failed")
    if principal.claims.get("identity_provider") != "workforce":
        raise _fail("fixture_invited_token_provider_not_signed")
    if principal.subject != seeded_subject:
        raise _evidence(
            "fixture_invited_login_landed_on_a_second_account",
            {"seeded": seeded_subject, "signed_in_as": principal.subject},
        )

    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        broker_admin = await _admin_token(client, BROKER)
        after = await _invited_accounts(client, admin_token=broker_admin)
        if [item["subject"] for item in after] != [seeded_subject]:
            raise _evidence("fixture_invited_login_created_a_second_account", after)
        linked_shape = after[0]
        if linked_shape["federated_identities"] != ["workforce"]:
            raise _evidence("fixture_invited_login_left_the_account_unlinked", linked_shape)
        if linked_shape["email_verified"] is not True:
            raise _evidence(
                "fixture_invited_login_left_the_address_unproven",
                linked_shape,
            )
        if linked_shape["credential_types"]:
            raise _evidence("fixture_invited_login_installed_a_credential", linked_shape)

        headers = {"Authorization": f"Bearer {broker_admin}"}
        removed = await client.delete(
            f"{BROKER}/admin/realms/akb/users/{seeded_subject}",
            headers=headers,
        )
        if removed.status_code != 204:
            raise _fail("fixture_invited_seed_remove_failed")
        upstream_removed = await client.delete(
            f"{UPSTREAM}/admin/realms/workforce/users/{upstream_subject}",
            headers={"Authorization": f"Bearer {await _admin_token(client, UPSTREAM)}"},
        )
        if upstream_removed.status_code != 204:
            raise _fail("fixture_invited_upstream_remove_failed")
        if await _invited_accounts(client, admin_token=broker_admin):
            raise _fail("fixture_invited_cleanup_not_observed")

    return {
        "seeded_account": "no-credential-no-required-action-unverified-unlinked",
        "pages_beyond_the_upstream_credential": 0,
        "signed_in_as": "the-seeded-account",
        "email_verified_after_first_login": True,
        "cleanup": "seed-and-upstream-removed",
    }


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

    # Prove where the authority to mint an administrator credential lives,
    # before the broker chain adds a second realm to reason about. It touches
    # only the product-administrator account and the temporary client the runner
    # created, so it is independent of everything below it.
    authority_boundary = await _prove_transient_authority_boundary()

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
    if principal.claims.get("identity_provider") != "workforce":
        raise _fail("fixture_broker_token_provider_not_signed")
    broker_id_claims = await keycloak_oidc.get_keycloak_oidc().verify_id_token(
        broker_tokens["id_token"],
        client_id="fixture-browser",
    )
    if broker_id_claims.get("identity_provider") != "workforce":
        raise _fail("fixture_broker_id_token_provider_not_signed")
    broker_user = await auth_service.project_verified_principal(principal)
    if broker_user is None or broker_user.user_id != str(state["user_id"]):
        raise _fail("fixture_broker_token_projection_failed")
    await _assert_continuity(
        state,
        upstream_subject=upstream_subject,
        broker_subject=broker_subject,
        expect_broker_binding=True,
    )

    # Everything above measures someone an operator linked by hand. This
    # measures someone an invitation seeded and nobody linked, which is the
    # only shape member invitation actually produces.
    invitation = await _prove_invitation_seeded_account_links()

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
                "schema_version": 4,
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
                "authority_boundary": authority_boundary,
                "invitation_seeded_account": invitation,
                "client_secret_exposed": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
