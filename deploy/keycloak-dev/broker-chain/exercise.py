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
from app.exceptions import AKBError
from app.services import auth_service, keycloak_oidc
from app.services.access_service import check_vault_access
from app.services.auth_verifier_profiles import verify_keycloak_access_v1
from app.services.admission_service import (
    approve_pending_admission,
    list_pending_admissions,
)
from app.services.role_sync import RoleSync, set_role_sync
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
    # Approving an arrival creates an AKB account, and account creation calls
    # the process-global RoleSync the way it does in a deployment. Standing it
    # up here rather than stubbing it keeps the approval path the real one.
    set_role_sync(RoleSync(pool))
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
# One address per person, because the broker realm's collision detection is on
# the address and two people sharing one would make each half's outcome depend
# on the other's leftovers.
SEEDED_EMAIL = "seeded-probe@example.com"
SEEDED_UPSTREAM_USERNAME = "seeded-probe"
SEEDED_UPSTREAM_PASSWORD = "fixture-only-seeded-probe-password"  # pragma: allowlist secret
INVITED_EMAIL = "invitee@example.com"
INVITED_UPSTREAM_USERNAME = "invitee"
INVITED_UPSTREAM_PASSWORD = "fixture-only-invitee-password"  # pragma: allowlist secret
MIGRANT_EMAIL = "migrant@example.com"
MIGRANT_UPSTREAM_USERNAME = "migrant"
MIGRANT_UPSTREAM_PASSWORD = "fixture-only-migrant-password"  # pragma: allowlist secret


def _seeded_account_payload() -> dict[str, Any]:
    """The shape member invitation used to create, field for field.

    Username and email are the same value because Keycloak's first-broker-login
    detects a collision with an existing account on either key. No credential
    and no required action, so nothing can sign into it directly. Not marked
    verified, because verification is a statement about what THIS realm has
    proved and this realm has proved nothing about this address.

    Kept, rather than deleted with the ceremony, because the dead end below is
    the reason the ceremony is switched off. A comment saying "seeding does not
    work" is not evidence; a phase that seeds and shows where the person stops
    is.
    """
    return {
        "username": SEEDED_EMAIL,
        "email": SEEDED_EMAIL,
        "enabled": True,
        "emailVerified": False,
    }


def _evidence(code: str, evidence: object) -> RuntimeError:
    """A failure that says what was on screen, not just that something failed.

    The codes in this fixture are normally enough because the step that raises
    them is the whole story. These phases measure another system's behaviour,
    so a bare code would report that an expectation was missed without
    reporting what Keycloak actually did instead.
    """
    return RuntimeError(f"{code} {json.dumps(evidence, sort_keys=True)}")


def _page_shape(response: httpx.Response) -> dict[str, object]:
    """Name the page a browser is being shown, in fields rather than prose.

    The rendered text is theme markup and inline script; the form fields are
    the meaning. A username/password pair on the upstream host is the person's
    own credential. The same pair on the broker host is Keycloak demanding that
    they re-authenticate as the account being linked to -- which for a seeded
    account means proving a credential that was never created. A `submitAction`
    button is Keycloak asking which account this login belongs to.

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


async def _arrive_through_the_broker(username: str, password: str) -> dict[str, object]:
    """Drive one person's sign-in, supplying only what that person holds.

    An invited person holds exactly one thing: their credential in the
    workspace's upstream directory. So this supplies exactly that, exactly
    once, and stops at the next page that asks for anything else -- because a
    step they cannot complete is a failure and not a slow success, and which
    page it is IS the finding.
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
                    raise _fail("fixture_arrival_redirect_invalid")
                target = urljoin(str(response.url), location)
                if target.startswith(redirect_uri):
                    query = parse_qs(urlsplit(target).query)
                    if "error" in query:
                        return {"pages": pages, "access_token": None, "rejected": True}
                    values = query.get("code", [])
                    if len(values) != 1 or not values[0]:
                        raise _fail("fixture_arrival_code_missing")
                    code = values[0]
                    break
                response = await client.get(target)
                continue
            if response.status_code != 200:
                raise _fail("fixture_arrival_flow_failed")
            page = _page_shape(response)
            pages.append(page)
            if page["kind"] != "upstream-credential-form" or supplied_credential:
                break
            action, data = _login_form(response)
            data.update(
                {
                    "username": username,
                    "password": password,
                    "login": data.get("login") or "Sign In",
                }
            )
            response = await client.post(action, data=data)
            supplied_credential = True
        if code is None:
            return {"pages": pages, "access_token": None}
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
        raise _fail("fixture_arrival_code_exchange_failed")
    tokens = _require_object(token_response.json(), "fixture_arrival_token_invalid")
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str):
        raise _fail("fixture_arrival_token_invalid")
    return {"pages": pages, "access_token": access_token}


async def _realm_accounts(
    client: httpx.AsyncClient,
    *,
    admin_token: str,
    email: str,
) -> list[dict[str, Any]]:
    """Every broker-realm account holding one address, with its shape.

    Read by address rather than by id on purpose. The id is what a binding
    names, so asking Keycloak "which accounts answer to this address" is the
    only question that can reveal a second one.
    """
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.get(
        f"{BROKER}/admin/realms/akb/users",
        params={"email": email, "exact": "true"},
        headers=headers,
    )
    if response.status_code != 200:
        raise _fail("fixture_realm_account_read_failed")
    accounts: list[dict[str, Any]] = []
    for user in _require_objects(response.json(), "fixture_realm_account_read_failed"):
        subject = user.get("id")
        if not isinstance(subject, str):
            raise _fail("fixture_realm_account_read_failed")
        credentials = await client.get(
            f"{BROKER}/admin/realms/akb/users/{subject}/credentials",
            headers=headers,
        )
        links = await client.get(
            f"{BROKER}/admin/realms/akb/users/{subject}/federated-identity",
            headers=headers,
        )
        if credentials.status_code != 200 or links.status_code != 200:
            raise _fail("fixture_realm_account_read_failed")
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
                        "fixture_realm_account_read_failed",
                    )
                ),
                "federated_identities": sorted(
                    str(item.get("identityProvider"))
                    for item in _require_objects(
                        links.json(),
                        "fixture_realm_account_read_failed",
                    )
                ),
            }
        )
    return accounts


async def _create_upstream_person(
    client: httpx.AsyncClient,
    *,
    username: str,
    email: str,
    password: str,
) -> str:
    created = await client.post(
        f"{UPSTREAM}/admin/realms/workforce/users",
        headers={"Authorization": f"Bearer {await _admin_token(client, UPSTREAM)}"},
        json={
            "username": username,
            "email": email,
            "emailVerified": True,
            "enabled": True,
            "firstName": "Fixture",
            "lastName": "Person",
            "credentials": [
                {"type": "password", "value": password, "temporary": False}
            ],
        },
    )
    if created.status_code != 201:
        raise _fail("fixture_upstream_create_failed")
    subject = urlsplit(created.headers.get("location") or "").path.rsplit("/", 1)[-1]
    if not subject:
        raise _fail("fixture_upstream_location_invalid")
    return subject


async def _remove_person(
    client: httpx.AsyncClient,
    *,
    upstream_subject: str,
    broker_subjects: list[str],
) -> None:
    broker_admin = await _admin_token(client, BROKER)
    for subject in broker_subjects:
        removed = await client.delete(
            f"{BROKER}/admin/realms/akb/users/{subject}",
            headers={"Authorization": f"Bearer {broker_admin}"},
        )
        if removed.status_code != 204:
            raise _fail("fixture_broker_account_remove_failed")
    removed = await client.delete(
        f"{UPSTREAM}/admin/realms/workforce/users/{upstream_subject}",
        headers={"Authorization": f"Bearer {await _admin_token(client, UPSTREAM)}"},
    )
    if removed.status_code != 204:
        raise _fail("fixture_upstream_remove_failed")


async def _prove_seeding_is_a_dead_end() -> dict[str, object]:
    """Assert where an account seeded ahead of arrival stops its owner.

    Member invitation used to create the account in the workspace realm before
    the person arrived, deliberately giving it no credential -- because a member
    holding a realm-native password keeps it after their organisation revokes
    the upstream account that was supposed to govern them. The stated ground for
    leaving the address unverified was that the first accepted login through the
    broker would set the flag from the upstream's signed proof.

    That was a claim about Keycloak, and this is the measurement. It seeds the
    exact shape, checks every property of it rather than assuming it, supplies
    the invited person's upstream credential and nothing else, and asserts the
    page they are stopped on. Keycloak's stock first-broker-login finds the
    seeded account by address and stops on confirm-link -- "User with email ...
    already exists" -- and neither branch out of it reaches that account:
    "Add to existing account" asks for a credential this account has never had,
    and this deployment has no SMTP for the verify-by-email alternative, while
    changing the address completes the login onto a SECOND account.

    This is asserted rather than left as a comment because the seeding ceremony
    is still in the codebase behind a switch, correct only for the case where
    the platform is itself the upstream. The next person to read that switch
    should find the measurement, not the intention.
    """
    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        upstream_subject = await _create_upstream_person(
            client,
            username=SEEDED_UPSTREAM_USERNAME,
            email=SEEDED_EMAIL,
            password=SEEDED_UPSTREAM_PASSWORD,
        )
        broker_admin = await _admin_token(client, BROKER)
        seeded = await client.post(
            f"{BROKER}/admin/realms/akb/users",
            headers={"Authorization": f"Bearer {broker_admin}"},
            json=_seeded_account_payload(),
        )
        if seeded.status_code != 201:
            raise _fail("fixture_seed_failed")
        seeded_subject = urlsplit(seeded.headers.get("location") or "").path.rsplit(
            "/", 1
        )[-1]
        if not seeded_subject:
            raise _fail("fixture_seed_location_invalid")

        # The shape is the precondition, and it is checked rather than assumed.
        # Each property is a way for the dead end to be reached for some other
        # reason: a credential would let the person answer the demand, and a
        # federated identity link would skip first-broker-login altogether.
        before = await _realm_accounts(client, admin_token=broker_admin, email=SEEDED_EMAIL)
        if [item["subject"] for item in before] != [seeded_subject]:
            raise _evidence("fixture_seeded_account_not_exact", before)
        shape = before[0]
        if shape["enabled"] is not True:
            raise _evidence("fixture_seeded_account_not_enabled", shape)
        if shape["email_verified"] is not False:
            raise _evidence("fixture_seeded_account_email_verified", shape)
        if shape["credential_types"]:
            raise _evidence("fixture_seeded_account_carries_credential", shape)
        if shape["required_actions"]:
            raise _evidence("fixture_seeded_account_has_required_action", shape)
        if shape["federated_identities"] or shape["federation_link"]:
            raise _evidence("fixture_seeded_account_is_prelinked", shape)

    arrival = await _arrive_through_the_broker(
        SEEDED_UPSTREAM_USERNAME, SEEDED_UPSTREAM_PASSWORD
    )
    pages = arrival["pages"]
    assert isinstance(pages, list)
    if arrival["access_token"] is not None:
        raise _evidence("fixture_seeded_login_was_not_stopped", pages)
    if len(pages) != 2:
        raise _evidence("fixture_seeded_login_stopped_somewhere_else", pages)
    stop = pages[1]
    if (
        stop["kind"] != "confirm-link"
        or stop["host"] != urlsplit(BROKER).netloc
        or stop["path"] != "/realms/akb/login-actions/first-broker-login"
    ):
        raise _evidence("fixture_seeded_login_stopped_somewhere_else", pages)

    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        broker_admin = await _admin_token(client, BROKER)
        after = await _realm_accounts(client, admin_token=broker_admin, email=SEEDED_EMAIL)
        # Stopped means stopped: the flag the claim was about is still false,
        # nothing was linked, and no second account was created on the way.
        if [item["subject"] for item in after] != [seeded_subject]:
            raise _evidence("fixture_seeded_login_changed_the_realm", after)
        if after[0]["email_verified"] is not False or after[0]["federated_identities"]:
            raise _evidence("fixture_seeded_login_changed_the_account", after[0])
        await _remove_person(
            client,
            upstream_subject=upstream_subject,
            broker_subjects=[seeded_subject],
        )
        if await _realm_accounts(client, admin_token=broker_admin, email=SEEDED_EMAIL):
            raise _fail("fixture_seeded_cleanup_not_observed")

    return {
        "seeded_account": "no-credential-no-required-action-unverified-unlinked",
        "stopped_at": "confirm-link",
        "pages_shown": [str(page["kind"]) for page in pages],
        "reachable": False,
        "email_verified_after": False,
    }


async def _recorded_arrivals() -> list[dict[str, Any]]:
    listed = await list_pending_admissions()
    admissions = listed.get("pending_admissions")
    if not isinstance(admissions, list):
        raise _fail("fixture_pending_admission_list_invalid")
    return [_require_object(item, "fixture_pending_admission_list_invalid") for item in admissions]


async def _admitted_arrival(username: str, password: str) -> tuple[str, str]:
    """One arrival that AKB refuses, returning the subject and the record id.

    The subject is read out of a token this runtime's own verifier accepted,
    not out of an admin read, because the pair an approval binds must be the
    pair a token actually carries.
    """
    arrival = await _arrive_through_the_broker(username, password)
    pages = arrival["pages"]
    assert isinstance(pages, list)
    beyond = [page for page in pages if page["kind"] != "upstream-credential-form"]
    if beyond or len(pages) != 1:
        raise _evidence("fixture_arrival_needed_more_than_the_upstream_credential", pages)
    access_token = arrival["access_token"]
    if not isinstance(access_token, str):
        raise _evidence("fixture_arrival_produced_no_token", pages)
    principal = await verify_keycloak_access_v1(access_token, "api")
    if principal is None:
        raise _fail("fixture_arrival_token_verification_failed")
    if principal.claims.get("identity_provider") != "workforce":
        raise _fail("fixture_arrival_provider_not_signed")
    if principal.issuer != BROKER_ISSUER:
        raise _fail("fixture_arrival_issuer_not_the_broker")

    # Refused: the product answers nothing, exactly as before this work.
    if await auth_service.project_verified_principal(principal) is not None:
        raise _fail("fixture_arrival_was_admitted_without_approval")
    # ...and refused for the stated reason. The boundary above answers None to
    # every rejection there is -- suspended, conflicting, provider disabled --
    # so "refused" alone would be satisfied by an account that is broken rather
    # than merely unadmitted, and the phase would report a chain it never ran.
    # The resolver underneath it is the only place the code is visible from
    # here; the fixture is not running the HTTP surface that would carry it.
    try:
        await auth_service._resolve_or_provision_keycloak_user(  # noqa: SLF001
            dict(principal.claims)
        )
    except AKBError as refusal:
        if getattr(refusal, "code", None) != "membership_required":
            raise _evidence(
                "fixture_arrival_refused_for_another_reason",
                {"code": getattr(refusal, "code", None)},
            ) from None
    else:
        raise _fail("fixture_arrival_was_admitted_without_approval")

    recorded = [
        item
        for item in await _recorded_arrivals()
        if item.get("subject") == principal.subject
    ]
    if len(recorded) != 1:
        raise _evidence(
            "fixture_arrival_was_not_recorded",
            {"subject": principal.subject, "held": len(recorded)},
        )
    note = recorded[0]
    if note.get("issuer") != BROKER_ISSUER:
        raise _evidence("fixture_arrival_recorded_under_another_issuer", note)
    admission_id = note.get("id")
    if not isinstance(admission_id, str):
        raise _evidence("fixture_arrival_record_has_no_id", note)
    return principal.subject, admission_id


async def _prove_admission_chain(state: dict[str, object]) -> dict[str, object]:
    """The whole chain, against two real Keycloaks and this runtime's own code.

    Nothing is seeded. The invited person signs in through the upstream they
    already have; the broker mints their subject; ``invite_only`` refuses them
    and the arrival is recorded with that exact pair; an administrator approves
    that row; they sign in again and they are in.

    It also proves the two properties the pre-boundary workspace migration
    depends on, because that migration is the same three steps: approving with
    ``existing_user_id`` keeps the AKB account a person already has -- with its
    token, its owned vault and its writer grant -- and adds a binding beside the
    one they arrived with rather than replacing it; and a person who signs in
    twice before anyone approves them produces one record, not two.
    """
    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        invited_upstream = await _create_upstream_person(
            client,
            username=INVITED_UPSTREAM_USERNAME,
            email=INVITED_EMAIL,
            password=INVITED_UPSTREAM_PASSWORD,
        )
        migrant_upstream = await _create_upstream_person(
            client,
            username=MIGRANT_UPSTREAM_USERNAME,
            email=MIGRANT_EMAIL,
            password=MIGRANT_UPSTREAM_PASSWORD,
        )
        broker_admin = await _admin_token(client, BROKER)
        # The precondition that separates this from the phase above: the realm
        # holds nothing for either address. Whatever the broker finds, it will
        # have made itself.
        for email in (INVITED_EMAIL, MIGRANT_EMAIL):
            if await _realm_accounts(client, admin_token=broker_admin, email=email):
                raise _fail("fixture_admission_precondition_realm_not_empty")

    if await _recorded_arrivals():
        raise _fail("fixture_admission_precondition_records_not_empty")

    # ── the invited person ────────────────────────────────────────────────
    invited_subject, invited_admission = await _admitted_arrival(
        INVITED_UPSTREAM_USERNAME, INVITED_UPSTREAM_PASSWORD
    )

    # A second attempt before anyone approves is the same person knocking
    # again, not a second arrival: the table is bounded by who, not how often.
    repeat_subject, repeat_admission = await _admitted_arrival(
        INVITED_UPSTREAM_USERNAME, INVITED_UPSTREAM_PASSWORD
    )
    if (repeat_subject, repeat_admission) != (invited_subject, invited_admission):
        raise _evidence(
            "fixture_repeat_arrival_made_a_second_record",
            {"first": invited_admission, "second": repeat_admission},
        )

    approved = await approve_pending_admission(
        invited_admission,
        actor_id="fixture-product-admin",
    )
    invited_user_id = _require_object(
        approved.get("user"), "fixture_approval_readback_invalid"
    ).get("user_id")
    if not isinstance(invited_user_id, str):
        raise _fail("fixture_approval_readback_invalid")

    # Signing in again is the whole point. Same person, same credential, and
    # this time the product answers with their account.
    second = await _arrive_through_the_broker(
        INVITED_UPSTREAM_USERNAME, INVITED_UPSTREAM_PASSWORD
    )
    second_token = second["access_token"]
    if not isinstance(second_token, str):
        raise _evidence("fixture_admitted_login_produced_no_token", second["pages"])
    second_principal = await verify_keycloak_access_v1(second_token, "api")
    if second_principal is None or second_principal.subject != invited_subject:
        raise _fail("fixture_admitted_login_landed_elsewhere")
    admitted = await auth_service.project_verified_principal(second_principal)
    if admitted is None:
        raise _fail("fixture_admitted_login_was_refused")
    if admitted.user_id != invited_user_id:
        raise _evidence(
            "fixture_admitted_login_resolved_to_another_account",
            {"approved": invited_user_id, "signed_in_as": admitted.user_id},
        )
    if any(item.get("id") == invited_admission for item in await _recorded_arrivals()):
        raise _fail("fixture_approved_arrival_is_still_pending")

    # ── the person who already has an account here ────────────────────────
    # Exactly the migration's shape: someone whose AKB account, token and
    # bindings predate this workspace owning an identity provider. Their own
    # account rather than the continuity fixture's, so that what this phase
    # writes cannot make the rollback assertions below pass or fail.
    pool = state["pool"]
    assert hasattr(pool, "acquire")
    migrant_user_id = uuid.uuid4()
    migrant_raw_pat, migrant_hash, migrant_prefix = auth_service.generate_pat()
    async with pool.acquire() as conn:  # type: ignore[union-attr]
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO users (
                    id, username, email, password_hash, display_name, is_admin,
                    auth_provider, account_status, account_kind
                ) VALUES ($1, 'migrant', $2, $3, 'Migrant', false,
                          'keycloak', 'active', 'human')
                """,
                migrant_user_id,
                MIGRANT_EMAIL,
                "!keycloak-sso:no-local-login!",  # pragma: allowlist secret
            )
            await conn.execute(
                """
                INSERT INTO external_identities (
                    user_id, issuer, subject, username_snapshot, email_snapshot
                ) VALUES ($1, $2, $3, 'migrant', $4)
                """,
                migrant_user_id,
                UPSTREAM_ISSUER,
                migrant_upstream,
                MIGRANT_EMAIL,
            )
            await conn.execute(
                """
                INSERT INTO tokens (
                    id, user_id, name, token_hash, token_prefix, scopes, key_class
                ) VALUES ($1, $2, 'migrant-pat', $3, $4, ARRAY['read'], 'pat')
                """,
                uuid.uuid4(),
                migrant_user_id,
                migrant_hash,
                migrant_prefix,
            )

    migrant_subject, migrant_admission = await _admitted_arrival(
        MIGRANT_UPSTREAM_USERNAME, MIGRANT_UPSTREAM_PASSWORD
    )
    await approve_pending_admission(
        migrant_admission,
        actor_id="fixture-product-admin",
        existing_user_id=str(migrant_user_id),
    )
    migrated = await _arrive_through_the_broker(
        MIGRANT_UPSTREAM_USERNAME, MIGRANT_UPSTREAM_PASSWORD
    )
    migrated_token = migrated["access_token"]
    if not isinstance(migrated_token, str):
        raise _evidence("fixture_migrated_login_produced_no_token", migrated["pages"])
    migrated_principal = await verify_keycloak_access_v1(migrated_token, "api")
    if migrated_principal is None or migrated_principal.subject != migrant_subject:
        raise _fail("fixture_migrated_login_landed_elsewhere")
    migrated_user = await auth_service.project_verified_principal(migrated_principal)
    if migrated_user is None or migrated_user.user_id != str(migrant_user_id):
        raise _fail("fixture_migrated_login_did_not_keep_the_account")

    # The old binding is still there beside the new one -- that overlap is what
    # makes the move reversible -- and the token they were already using still
    # authorizes, which is the half a re-created account would silently lose.
    async with pool.acquire() as conn:  # type: ignore[union-attr]
        bindings = await conn.fetch(
            """
            SELECT issuer, subject FROM external_identities
             WHERE user_id = $1 ORDER BY issuer, subject
            """,
            migrant_user_id,
        )
    if sorted((row["issuer"], row["subject"]) for row in bindings) != sorted(
        [(BROKER_ISSUER, migrant_subject), (UPSTREAM_ISSUER, migrant_upstream)]
    ):
        raise _evidence(
            "fixture_migrated_bindings_not_both_present",
            [[row["issuer"], row["subject"]] for row in bindings],
        )
    pat_user = await auth_service.resolve_rest_user_authorization(
        f"Bearer {migrant_raw_pat}"
    )
    if pat_user is None or pat_user.user_id != str(migrant_user_id):
        raise _fail("fixture_migrated_pat_continuity_failed")

    # ── clean up, and prove the cleanup ───────────────────────────────────
    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        await _remove_person(
            client,
            upstream_subject=invited_upstream,
            broker_subjects=[invited_subject],
        )
        await _remove_person(
            client,
            upstream_subject=migrant_upstream,
            broker_subjects=[migrant_subject],
        )
        broker_admin = await _admin_token(client, BROKER)
        for email in (INVITED_EMAIL, MIGRANT_EMAIL):
            if await _realm_accounts(client, admin_token=broker_admin, email=email):
                raise _fail("fixture_admission_cleanup_not_observed")
    if await _recorded_arrivals():
        raise _fail("fixture_admission_records_not_drained")
    async with pool.acquire() as conn:  # type: ignore[union-attr]
        await conn.execute(
            "DELETE FROM users WHERE id = ANY($1::uuid[])",
            [uuid.UUID(invited_user_id), migrant_user_id],
        )

    return {
        "seeded": "nothing",
        "pages_beyond_the_upstream_credential": 0,
        "refused_before_approval": "membership_required",
        "arrival_recorded_as": "exact-broker-issuer-and-subject",
        "repeat_arrival": "one-record",
        "approved_then_admitted": True,
        "existing_account_preserved": True,
        "binding_added_beside_the_old_one": True,
        "records_drained": True,
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

    # Everything above measures someone an operator linked by hand. These two
    # measure the people nobody linked: the shape member invitation used to
    # create, and the shape admission produces.
    seeding_dead_end = await _prove_seeding_is_a_dead_end()
    admission = await _prove_admission_chain(state)

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
                "schema_version": 5,
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
                "seeding_dead_end": seeding_dead_end,
                "admission_chain": admission,
                "client_secret_exposed": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
