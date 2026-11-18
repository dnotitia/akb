#!/usr/bin/env python3
"""Provision the AKB public native-client registration policy in Keycloak.

Run against a realm on Keycloak 26.7 with only the OpenID Connect client
registration provider enabled. Configure the server with
--spi-client-registration--default--enabled=false and
--spi-client-registration--saml2-entity-descriptor--enabled=false and
--features-disabled=device-flow.

Use KC_ADMIN_CLIENT_ID / KC_ADMIN_CLIENT_SECRET or KC_ADMIN_USER /
KC_ADMIN_PASS for the admin credential. KC_ADMIN_REALM defaults to master.

    python3 scripts/keycloak/setup-akb-mcp-oauth.py \
        --kc https://auth.example.com --realm akb \
        --audience https://akb.example.com/mcp

See docs/mcp-clients/web-connectors.md for the authorization-server contract.
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


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(RejectRedirects)


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
        with _opener.open(req, timeout=10) as r:
            raw = r.read().decode()
            payload = json.loads(raw) if raw and r.headers.get_content_type() == "application/json" else raw
            return r.status, payload, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


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
        raise ProvisioningError(f"Keycloak admin authentication failed: HTTP {status}")
    tok = payload.get("access_token")
    if not tok:
        raise ProvisioningError("Keycloak admin authentication returned no access token")
    return tok


SCOPE_POLICY = "allowed-client-templates"
POLICY_TYPE = "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
TOOL_SCOPES = {
    "akb:vault:read": "Read your AKB vaults (documents, tables, files, search)",
    "akb:vault:write": "Create, edit, and delete AKB content",
}
DCR_SCOPES = ["basic", "email", "profile", *TOOL_SCOPES]
LOOPBACK_PATTERN = r"^http://127\.0\.0\.1/oauth/callback$"
OTHER_URI_FIELDS = [
    "rootUrl", "adminUrl", "baseUrl", "webOrigins", "jwksUri", "requestUris",
    "backchannelLogoutUrl", "postLogoutRedirectUris", "cibaClientNotificationEndpoint",
    "logoUri", "policyUri", "tosUri", "sectorIdentifierUri",
]


class ProvisioningError(RuntimeError):
    pass


def api(method, url, token, *, body=None, statuses=(200,)):
    status, payload, _ = http(method, url, token, body=body)
    if status not in statuses:
        raise ProvisioningError(f"{method} {url} returned HTTP {status}")
    return payload


def upsert_by_name(collection, token, items, desired):
    matches = [item for item in items if item["name"] == desired["name"]]
    if len(matches) > 1:
        raise ProvisioningError(f"More than one {desired['name']} at {collection}")
    if not matches:
        api("POST", collection, token, body=desired, statuses=(201, 204))
        return
    current = matches[0]
    if any(current.get(key) != value for key, value in desired.items()):
        api("PUT", f"{collection}/{current['id']}", token, body=current | desired, statuses=(200, 204))


def read_tool_scopes(base, token):
    for kind in ("default", "optional"):
        for scope in api("GET", f"{base}/default-{kind}-client-scopes", token):
            if scope["name"] in TOOL_SCOPES:
                raise ProvisioningError(
                    f"Delete the realm {kind} client scope {scope['name']} before provisioning. "
                    "Recreate explicit client grants after running the script."
                )

    current = api("GET", f"{base}/client-scopes", token)
    for scope in current:
        name = scope["name"]
        if name in TOOL_SCOPES and scope.get("attributes", {}).get("akb.tool.scope") != "true":
            raise ProvisioningError(f"Delete the legacy scope {name} before provisioning")
    return current


def configure_scopes(base, token, audience, current):
    for name, consent in TOOL_SCOPES.items():
        collection = f"{base}/client-scopes"
        upsert_by_name(collection, token, current, {
            "name": name,
            "protocol": "openid-connect",
            "attributes": {
                "consent.screen.text": consent,
                "display.on.consent.screen": "true",
                "include.in.token.scope": "true",
                "akb.tool.scope": "true",
            },
        })
        scope = next(item for item in api("GET", collection, token) if item["name"] == name)
        collection = f"{collection}/{scope['id']}/protocol-mappers/models"
        for mapper_name, mapper_type, data in (
            ("akb-mcp-audience", "oidc-audience-mapper", {"included.custom.audience": audience}),
            ("akb-mcp-client-admission", "oidc-hardcoded-claim-mapper", {
                "claim.name": "akb_tool_client",
                "claim.value": "admitted",
                "jsonType.label": "String",
            }),
        ):
            upsert_by_name(collection, token, api("GET", collection, token), {
                "name": mapper_name,
                "protocol": "openid-connect",
                "protocolMapper": mapper_type,
                "config": data | {
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                    "userinfo.token.claim": "false",
                    "introspection.token.claim": "false",
                    "lightweight.claim": "false",
                },
            })


def read_registration_policies(base, token):
    policies = api("GET", f"{base}/components?type={POLICY_TYPE}", token)
    anonymous = [item for item in policies if item.get("subType") == "anonymous"]
    providers = {item.get("providerId") for item in anonymous}
    required_guards = {"allowed-protocol-mappers", "max-clients", "consent-required", "scope", "trusted-hosts"}
    if not required_guards.issubset(providers):
        raise ProvisioningError("Keycloak anonymous DCR requires the default registration safeguards")

    for subtype in ("anonymous", "authenticated"):
        if sum(item.get("providerId") == SCOPE_POLICY and item.get("subType") == subtype
               for item in policies) > 1:
            raise ProvisioningError(f"Multiple {subtype} client-scope policies")
    return policies


def configure_registration(base, realm, token, policies):
    collection = f"{base}/components"
    for subtype in ("anonymous", "authenticated"):
        matches = [item for item in policies if item.get("providerId") == SCOPE_POLICY
                   and item.get("subType") == subtype]
        allowed = DCR_SCOPES if subtype == "anonymous" else []
        desired = {
            "name": "Allowed Client Scopes",
            "providerId": SCOPE_POLICY,
            "providerType": POLICY_TYPE,
            "parentId": realm["id"],
            "subType": subtype,
            "config": {
                "allow-default-scopes": ["false"],
                "allowed-client-scopes": allowed,
            },
        }
        if matches:
            current = matches[0]
            config = current.get("config", {})
            if (
                config.get("allow-default-scopes") != ["false"]
                or set(config.get("allowed-client-scopes", ())) != set(allowed)
            ):
                api("PUT", f"{collection}/{current['id']}", token,
                    body=current | desired, statuses=(204,))
        else:
            api("POST", collection, token, body=desired, statuses=(201,))

    trusted = [item for item in policies if item.get("subType") == "anonymous"
               and item.get("providerId") == "trusted-hosts"]
    for current in trusted:
        desired = current | {"config": {
            "trusted-hosts": ["127.0.0.1"],
            "host-sending-registration-request-must-match": ["false"],
            "client-uris-must-match": ["true"],
        }}
        if desired != current:
            api("PUT", f"{collection}/{current['id']}", token, body=desired, statuses=(204,))


def _executor(provider, **configuration):
    return {"executor": provider, "configuration": configuration}


def _condition(provider, **configuration):
    return {"condition": provider, "configuration": configuration}


def _upsert_entries(endpoint, token, key, entries):
    current = api("GET", endpoint, token)
    previous = current.get(key, [])
    names = {entry["name"] for entry in entries}
    desired = [entry for entry in previous if entry["name"] not in names] + entries
    if desired != previous:
        api("PUT", endpoint, token, body={key: desired}, statuses=(200, 204))


def configure_client_policies(base, token):
    enforce_profile = "akb-dcr-native"
    uri_profile = "akb-dcr-client-metadata"
    reject_profile = "akb-dcr-reject"
    _upsert_entries(f"{base}/client-policies/profiles", token, "profiles", [
        {"name": enforce_profile, "executors": [
            _executor("secure-client-uris-pattern", **{
                "allowed-patterns": [LOOPBACK_PATTERN], "client-uri-fields": ["redirectUris"],
            }),
            _executor("pkce-enforcer", **{"auto-configure": True}),
            _executor("reject-implicit-grant", **{"auto-configure": False}),
            _executor("reject-ropc-grant", **{"auto-configure": False}),
        ]},
        {"name": uri_profile, "executors": [
            _executor("secure-client-uris-pattern", **{
                "allowed-patterns": ["(?!)"], "client-uri-fields": OTHER_URI_FIELDS,
            }),
        ]},
        {"name": reject_profile, "executors": [_executor("reject-request")]},
    ])
    anonymous = _condition("client-updater-context", **{"update-client-source": ["ByAnonymous"]})
    public_only = _condition("client-access-type", type=["confidential", "bearer-only"])
    registration_updates = _condition(
        "client-updater-context", **{"update-client-source": ["ByRegistrationAccessToken"]},
    )
    _upsert_entries(f"{base}/client-policies/policies", token, "policies", [
        {"name": "akb-dcr-public-only", "enabled": True, "conditions": [anonymous, public_only],
         "profiles": [reject_profile]},
        {"name": "akb-dcr-native", "enabled": True, "conditions": [anonymous],
         "profiles": [enforce_profile, uri_profile]},
        {"name": "akb-dcr-no-client-updates", "enabled": True, "conditions": [registration_updates],
         "profiles": [reject_profile]},
    ])


def require_native_registration(issuer: str) -> None:
    registration = f"{issuer}/clients-registrations"
    # The SAML registration provider exposes only its collection endpoint.
    probes = {"default": "default/akb-policy-probe", "saml2-entity-descriptor": "saml2-entity-descriptor"}
    for provider, path in probes.items():
        status, _, _ = http("GET", f"{registration}/{path}")
        if status != 404:
            raise ProvisioningError(f"Disable the alternate Keycloak registration provider {provider} before provisioning")
    status, metadata, _ = http("GET", f"{issuer}/.well-known/openid-configuration")
    if (
        status != 200 or not isinstance(metadata, dict)
        or "urn:ietf:params:oauth:grant-type:device_code" in metadata.get("grant_types_supported", ())
        or metadata.get("device_authorization_endpoint")
    ):
        raise ProvisioningError("Use readable Keycloak discovery with the device-flow feature disabled")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kc", required=True, help="Keycloak base URL")
    p.add_argument("--realm", required=True, help="Keycloak realm name")
    p.add_argument("--audience", required=True, help="Exact public MCP resource URL")
    args = p.parse_args()
    try:
        for url in (args.kc, args.audience):
            parsed = urllib.parse.urlsplit(url)
            if (
                not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or (parsed.scheme != "https" and not (
                    parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "::1", "localhost")
                ))
            ):
                raise ProvisioningError("Expected an HTTPS URL or a loopback HTTP URL without credentials")
        if not urllib.parse.urlsplit(args.audience).path.endswith("/mcp"):
            raise ProvisioningError("Audience must be the exact public MCP resource URL")
        kc = args.kc.rstrip("/")
        realm = urllib.parse.quote(args.realm, safe="")
        require_native_registration(f"{kc}/realms/{realm}")
        credential = resolve_admin_credential(os.environ)
        if credential.shadowed:
            print(f"Ignoring the alternative credential variables {' and '.join(credential.shadowed)}")
        token = get_admin_token(kc, credential)
        base = f"{kc}/admin/realms/{realm}"
        state = api("GET", base, token)
        scopes = read_tool_scopes(base, token)
        policies = read_registration_policies(base, token)
        configure_client_policies(base, token)
        configure_scopes(base, token, args.audience, scopes)
        configure_registration(base, state, token, policies)
        if not state.get("revokeRefreshToken") or state.get("refreshTokenMaxReuse") != 0:
            api("PUT", base, token, body={"revokeRefreshToken": True, "refreshTokenMaxReuse": 0},
                statuses=(204,))
    except (AdminCredentialError, ProvisioningError, urllib.error.URLError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Provisioned public native OAuth registration for {args.audience}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
