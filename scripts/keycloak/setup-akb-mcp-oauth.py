#!/usr/bin/env python3
"""Provision the public native AKB OAuth client in a Keycloak realm.

KC_ADMIN_CLIENT_ID + KC_ADMIN_CLIENT_SECRET authenticate an admin service
account. KC_ADMIN_USER + KC_ADMIN_PASS authenticate an admin user. Credentials
stay in the environment. KC_ADMIN_REALM defaults to master.

Usage:
    python3 scripts/keycloak/setup-akb-mcp-oauth.py --kc https://auth.example.com \
        --realm akb --audience https://akb.example.com/mcp \
        --native-client-id akb-mcp-native

This deliberately does not open dynamic registration policies. Direct HTTP
OAuth clients require a separate registration and explicit AKB allowlisting.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


HTTP_TIMEOUT_SECS = 10.0


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(method: str, url: str, token: str | None = None, body=None,
         ctype: str = "application/json") -> tuple[int, object, dict]:
    """Tiny stdlib HTTP wrapper — keeps the script dependency-free."""
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        if ctype == "application/json":
            data = json.dumps(body).encode()
        else:
            data = urllib.parse.urlencode(body).encode()
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.build_opener(RejectRedirects()).open(req, timeout=HTTP_TIMEOUT_SECS) as r:
            raw = r.read().decode()
            payload = json.loads(raw) if raw and r.headers.get_content_type() == "application/json" else raw
            return r.status, payload, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Keycloak request failed or timed out") from None


class AdminCredentialError(RuntimeError):
    """The environment does not carry a usable Keycloak admin credential."""


@dataclass(frozen=True)
class AdminCredential:
    """One resolved way to obtain an admin token, and how to ask for it.

    ``shadowed`` names the env vars of a second, also-complete credential
    that was not used, so a stale export in the operator's shell shows up
    in the run output instead of silently deciding which identity edits
    the realm.
    """

    kind: Literal["client_credentials", "password"]
    realm: str
    client_id: str
    client_secret: str | None = None
    username: str | None = None
    password: str | None = None
    shadowed: tuple[str, ...] = ()

    def describe(self) -> str:
        """Human-readable identity for logs. Never includes the secret."""
        if self.kind == "client_credentials":
            return f"client credentials as '{self.client_id}' in realm '{self.realm}'"
        return f"password grant as '{self.username}' on '{self.client_id}' in realm '{self.realm}'"

    def token_request_body(self) -> dict[str, str]:
        if self.kind == "client_credentials":
            return {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret or "",
            }
        return {
            "grant_type": "password",
            "client_id": self.client_id,
            "username": self.username or "",
            "password": self.password or "",
        }


# Both halves of a pair must be present. Naming the missing half is the
# whole point: "KC_ADMIN_CLIENT_SECRET is not set" is actionable, and a
# generic "credentials required" is what sent an operator looking for an
# admin user the deployment never created.
_CREDENTIAL_HELP = (
    "No Keycloak admin credential in the environment. Set ONE of these pairs "
    "temporarily for this run (the script never persists them):\n"
    "  KC_ADMIN_CLIENT_ID + KC_ADMIN_CLIENT_SECRET\n"
    "      Admin service account. This is what the Kubernetes manifests in this "
    "repo bootstrap (KC_BOOTSTRAP_ADMIN_CLIENT_ID / KC_BOOTSTRAP_ADMIN_CLIENT_SECRET "
    "in deploy/k8s/standalone-sso/keycloak.yaml and "
    "deploy/helm/akb/templates/sso.yaml); they create no admin user.\n"
    "  KC_ADMIN_USER + KC_ADMIN_PASS\n"
    "      Password grant on admin-cli. This is what the local dev fixture "
    "bootstraps (KC_BOOTSTRAP_ADMIN_USERNAME / KC_BOOTSTRAP_ADMIN_PASSWORD in "
    "deploy/keycloak-dev/broker-chain/compose.yaml).\n"
    "KC_ADMIN_REALM (default: master) names the realm the credential lives in, "
    "not the realm being configured."
)


def _present(value: str | None) -> bool:
    """A variable exported as empty or whitespace counts as unset."""
    return value is not None and value.strip() != ""


def resolve_admin_credential(env: Mapping[str, str]) -> AdminCredential:
    """Pick the admin credential to use, or raise naming what is missing.

    Client credentials win when both pairs are complete: it is the shape
    the shipped deployments actually produce, so preferring it makes the
    supported path the default one.
    """
    realm = env.get("KC_ADMIN_REALM", "").strip() or "master"
    client_id = env.get("KC_ADMIN_CLIENT_ID")
    client_secret = env.get("KC_ADMIN_CLIENT_SECRET")
    user = env.get("KC_ADMIN_USER")
    pwd = env.get("KC_ADMIN_PASS")

    client_pair = (_present(client_id), _present(client_secret))
    password_pair = (_present(user), _present(pwd))

    if any(client_pair) and not all(client_pair):
        missing = "KC_ADMIN_CLIENT_SECRET" if client_pair[0] else "KC_ADMIN_CLIENT_ID"
        raise AdminCredentialError(
            f"{missing} is not set. Client-credentials auth needs both "
            "KC_ADMIN_CLIENT_ID and KC_ADMIN_CLIENT_SECRET.\n\n" + _CREDENTIAL_HELP
        )
    if any(password_pair) and not all(password_pair):
        missing = "KC_ADMIN_PASS" if password_pair[0] else "KC_ADMIN_USER"
        raise AdminCredentialError(
            f"{missing} is not set. Password auth needs both KC_ADMIN_USER and "
            "KC_ADMIN_PASS.\n\n" + _CREDENTIAL_HELP
        )

    if all(client_pair):
        return AdminCredential(
            kind="client_credentials",
            realm=realm,
            client_id=(client_id or "").strip(),
            client_secret=client_secret,
            shadowed=("KC_ADMIN_USER", "KC_ADMIN_PASS") if all(password_pair) else (),
        )
    if all(password_pair):
        return AdminCredential(
            kind="password",
            realm=realm,
            client_id="admin-cli",
            username=(user or "").strip(),
            password=pwd,
        )
    raise AdminCredentialError(_CREDENTIAL_HELP)


def get_admin_token(kc_url: str, credential: AdminCredential) -> str:
    status, payload, _ = http(
        "POST",
        f"{kc_url}/realms/{credential.realm}/protocol/openid-connect/token",
        body=credential.token_request_body(),
        ctype="application/x-www-form-urlencoded",
    )
    if status != 200 or not isinstance(payload, dict):
        sys.exit(f"admin auth failed ({credential.describe()}): HTTP {status}")
    tok = payload.get("access_token")
    if not isinstance(tok, str) or not tok:
        sys.exit(f"admin auth ({credential.describe()}): no access_token in HTTP {status} response")
    return tok


NATIVE_REDIRECT_URI = "http://127.0.0.1/oauth/callback"
VAULT_SCOPES = ("akb:vault:read", "akb:vault:write")


def native_client(client_id: str, audience: str) -> dict:
    return {
        "clientId": client_id,
        "name": "AKB native MCP client",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": True,
        "bearerOnly": False,
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "fullScopeAllowed": False,
        "consentRequired": True,
        "redirectUris": [NATIVE_REDIRECT_URI],
        "webOrigins": [],
        "defaultClientScopes": ["profile", "email"],
        "optionalClientScopes": list(VAULT_SCOPES),
        "attributes": {"pkce.code.challenge.method": "S256", "use.refresh.tokens": "true"},
        "protocolMappers": [{
            "name": "akb-tool-resource",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "consentRequired": False,
            "config": {"included.custom.audience": audience, "access.token.claim": "true", "id.token.claim": "false"},
        }],
    }


def provision_native_client(base: str, token: str, client_id: str, audience: str, *, request=http) -> None:
    """Converge the pinned public registration, scopes and refresh rotation."""
    def call(method: str, path: str, body=None):
        status, payload, _headers = request(method, base + path, token, body=body)
        if status not in {200, 201, 204}:
            raise RuntimeError(f"Keycloak {method} {path} failed ({status})")
        return payload

    scopes = call("GET", "/client-scopes")
    for name in VAULT_SCOPES:
        scope = next((scope for scope in scopes if scope.get("name") == name), None)
        if scope is None:
            call("POST", "/client-scopes", {
                "name": name, "protocol": "openid-connect",
                "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "true",
                               "consent.screen.text": name},
            })
        else:
            scope["attributes"] = {**scope.get("attributes", {}), "include.in.token.scope": "true"}
            call("PUT", f"/client-scopes/{scope['id']}", scope)
            mappers = call("GET", f"/client-scopes/{scope['id']}/protocol-mappers/models")
            for mapper in mappers:
                if mapper.get("name") == "akb-mcp-audience" and mapper.get("protocolMapper") == "oidc-audience-mapper":
                    call("DELETE", f"/client-scopes/{scope['id']}/protocol-mappers/models/{mapper['id']}")

    desired = native_client(client_id, audience)
    clients = call("GET", "/clients?" + urllib.parse.urlencode({"clientId": client_id}))
    existing = [client for client in clients if client.get("clientId") == client_id]
    if len(existing) > 1:
        raise RuntimeError("Duplicate native client registration")
    if existing:
        identifier = existing[0]["id"]
        call("PUT", f"/clients/{identifier}", desired)
    else:
        call("POST", "/clients", desired)
        clients = call("GET", "/clients?" + urllib.parse.urlencode({"clientId": client_id}))
        identifier = next(client["id"] for client in clients if client.get("clientId") == client_id)

    # Keycloak stores scope associations and mappers in separate resources.
    scope_ids = {scope["name"]: scope["id"] for scope in call("GET", "/client-scopes")}
    for kind, names in [("default", desired["defaultClientScopes"]), ("optional", desired["optionalClientScopes"])]:
        assigned = call("GET", f"/clients/{identifier}/{kind}-client-scopes")
        for scope in assigned:
            if scope["name"] not in names:
                call("DELETE", f"/clients/{identifier}/{kind}-client-scopes/{scope['id']}")
        for name in names:
            if name not in scope_ids:
                raise RuntimeError(f"Required Keycloak client scope is missing: {name}")
            call("PUT", f"/clients/{identifier}/{kind}-client-scopes/{scope_ids[name]}")
    mappers = call("GET", f"/clients/{identifier}/protocol-mappers/models")
    for mapper in mappers:
        call("DELETE", f"/clients/{identifier}/protocol-mappers/models/{mapper['id']}")
    call("POST", f"/clients/{identifier}/protocol-mappers/models", desired["protocolMappers"][0])
    call("PUT", "", {"revokeRefreshToken": True, "refreshTokenMaxReuse": 0})

    actual = call("GET", f"/clients/{identifier}")
    for key, value in desired.items():
        if key not in {"protocolMappers", "defaultClientScopes", "optionalClientScopes", "attributes"} and actual.get(key) != value:
            raise RuntimeError(f"Native client verification failed for {key}")
    if actual.get("secret"):
        raise RuntimeError("Native client must have no secret")
    if any(actual.get("attributes", {}).get(key) != value for key, value in desired["attributes"].items()):
        raise RuntimeError("Native PKCE/refresh attributes differ")
    actual_mappers = call("GET", f"/clients/{identifier}/protocol-mappers/models")
    expected_mapper = desired["protocolMappers"][0]
    if len(actual_mappers) != 1 or any(actual_mappers[0].get(key) != value for key, value in expected_mapper.items()):
        raise RuntimeError("Native audience mapper differs")
    for kind, names in [("default", desired["defaultClientScopes"]), ("optional", desired["optionalClientScopes"])]:
        assigned = call("GET", f"/clients/{identifier}/{kind}-client-scopes")
        if {scope["name"] for scope in assigned} != set(names):
            raise RuntimeError(f"Native {kind} scopes differ")
    realm = call("GET", "")
    if realm.get("revokeRefreshToken") is not True or realm.get("refreshTokenMaxReuse") != 0:
        raise RuntimeError("Refresh rotation verification failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kc", required=True)
    parser.add_argument("--realm", required=True)
    parser.add_argument("--audience", required=True, help="Exact public_base_url + '/mcp'")
    parser.add_argument("--native-client-id", required=True)
    args = parser.parse_args()
    resource = urllib.parse.urlsplit(args.audience)
    if (not resource.hostname or resource.username or resource.password or resource.query or resource.fragment
            or not resource.path.endswith("/mcp") or resource.scheme not in {"http", "https"}
            or resource.scheme == "http" and resource.hostname not in {"127.0.0.1", "localhost", "::1"}):
        parser.error("audience must be the canonical HTTPS tool resource, or loopback HTTP for development")
    if not args.native_client_id or args.native_client_id != args.native_client_id.strip():
        parser.error("native-client-id must be nonempty without surrounding whitespace")
    try:
        credential = resolve_admin_credential(os.environ)
        token = get_admin_token(args.kc.rstrip("/"), credential)
        provision_native_client(f"{args.kc.rstrip('/')}/admin/realms/{urllib.parse.quote(args.realm, safe='')}",
                                token, args.native_client_id, args.audience)
    except (AdminCredentialError, RuntimeError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(f"Provisioned public native client {args.native_client_id} for {args.audience}")
    print("Refresh-token rotation is enabled realm-wide with maximum reuse 0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
