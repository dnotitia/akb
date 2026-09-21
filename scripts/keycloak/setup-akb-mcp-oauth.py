#!/usr/bin/env python3
"""Apply MCP OAuth Resource Server changes to a Keycloak realm.

Idempotent — running this twice is a no-op on the second call. Survives
partial state from a prior interrupted run.

What it does (in order):

1. Add ``localhost`` / ``127.0.0.1`` to the realm's DCR ``trusted-hosts``
   policy so a local Claude Code (or another DCR-capable MCP client
   running on the operator's laptop) can register itself dynamically,
   and turn that policy's sender-host check off.
1b. Delete the ``Allowed Client Scopes`` registration policy from BOTH
   the ``anonymous`` and the ``authenticated`` policy set — an Initial
   Access Token switches which set applies rather than bypassing it, so
   Protected DCR hits the same wall.
2. Create the ``akb:vault:read`` client scope (if absent) with an
   ``oidc-audience-mapper`` whose ``included.custom.audience`` is the
   AKB ``/mcp`` URL the realm should mint tokens for.
3. Create the ``akb:vault:write`` client scope the same way.
4. Add both scopes to ``defaultOptionalClientScopes`` so a DCR-registered
   public client can request them at the authorize endpoint.
5. Verify everything by re-reading state.

Reads the Keycloak admin credential from the environment; it is never
accepted on the command line, so it cannot land in shell history or a
process listing. Two credential shapes are supported, because the two
ways this repo ships Keycloak bootstrap two different kinds of admin:

* ``KC_ADMIN_CLIENT_ID`` / ``KC_ADMIN_CLIENT_SECRET`` — client
  credentials for an admin **service account**. This is what both
  Kubernetes paths create: ``deploy/k8s/standalone-sso/keycloak.yaml``
  and ``deploy/helm/akb/templates/sso.yaml`` set
  ``KC_BOOTSTRAP_ADMIN_CLIENT_ID`` / ``KC_BOOTSTRAP_ADMIN_CLIENT_SECRET``
  and create no admin user at all.
* ``KC_ADMIN_USER`` / ``KC_ADMIN_PASS`` — password grant on
  ``admin-cli``. This is what the local dev fixture creates
  (``deploy/keycloak-dev/broker-chain/compose.yaml`` sets
  ``KC_BOOTSTRAP_ADMIN_USERNAME`` / ``KC_BOOTSTRAP_ADMIN_PASSWORD``).

``KC_ADMIN_REALM`` (default ``master``) names the realm the credential
itself lives in, which is not the realm being configured: the bootstrap
admin is a ``master``-realm identity, while ``--realm`` is the realm
whose scopes and policies this script edits. Point it at the target
realm only if the admin service account was created there instead.

Usage:
    # Kubernetes (service account — the shipped manifests' bootstrap)
    KC_ADMIN_CLIENT_ID=akb-bootstrap-temporary KC_ADMIN_CLIENT_SECRET=... \\
        python3 scripts/keycloak/setup-akb-mcp-oauth.py \\
            --kc https://auth.example.com \\
            --realm akb \\
            --audience https://akb.example.com/mcp

    # Local dev compose fixture (admin user)
    KC_ADMIN_USER=admin KC_ADMIN_PASS=... \\
        python3 scripts/keycloak/setup-akb-mcp-oauth.py \\
            --kc https://auth.example.com \\
            --realm akb \\
            --audience https://akb.example.com/mcp

See docs/mcp-clients/web-connectors.md for the end-to-end client
walkthrough (it is the operational authority for realm settings) and
docs/designs/mcp-oauth-dcr/00-overview.md for the design rationale.
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
        with urllib.request.urlopen(req) as r:
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


CLIENT_SCOPE_POLICY_PROVIDER = "allowed-client-templates"


def client_scope_policies(components: object) -> list[dict]:
    """Every "Allowed Client Scopes" registration policy in a realm, both subtypes.

    Keycloak ships this policy twice: ``anonymous`` gates open DCR, and
    ``authenticated`` gates DCR made with an Initial Access Token. Both
    reject a spec-compliant DCR body for the same reason — it contains
    ``scope=openid``, and ``openid`` is the OIDC sentinel rather than an
    entry in the realm's client-scope catalog, so no configuration of the
    policy can permit it. Removing only the anonymous one leaves the
    Protected-DCR path (the one an operator hardening an
    internet-reachable IdP is told to use) failing exactly the way this
    script exists to fix.
    """
    if not isinstance(components, list):
        return []
    return [
        component
        for component in components
        if isinstance(component, dict)
        and component.get("providerId") == CLIENT_SCOPE_POLICY_PROVIDER
    ]


def _policy_subtypes(components: object) -> list[str]:
    """Subtypes of the client-scope policies still present. For the verify step."""
    return [str(p.get("subType") or "unknown") for p in client_scope_policies(components)]


def get_admin_token(kc_url: str, credential: AdminCredential) -> str:
    status, payload, _ = http(
        "POST",
        f"{kc_url}/realms/{credential.realm}/protocol/openid-connect/token",
        body=credential.token_request_body(),
        ctype="application/x-www-form-urlencoded",
    )
    if status != 200 or not isinstance(payload, dict):
        sys.exit(f"admin auth failed ({credential.describe()}): {status} {payload}")
    tok = payload.get("access_token")
    if not tok:
        sys.exit(f"admin auth ({credential.describe()}): no access_token in {payload}")
    return tok


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kc", required=True, help="Keycloak base URL, e.g. https://auth.example.com")
    p.add_argument("--realm", required=True, help="Keycloak realm name, e.g. akb")
    p.add_argument(
        "--audience", required=True,
        help="MCP resource identifier baked into the audience mapper, e.g. https://akb.example.com/mcp",
    )
    p.add_argument(
        "--trusted-host", action="append", default=["localhost", "127.0.0.1"],
        help="Hostname(s) to add to the DCR trusted-hosts policy (repeatable; defaults: localhost, 127.0.0.1)",
    )
    p.add_argument(
        "--scope-read", default="akb:vault:read",
        help="Read scope name (default: akb:vault:read)",
    )
    p.add_argument(
        "--scope-write", default="akb:vault:write",
        help="Write scope name (default: akb:vault:write)",
    )
    args = p.parse_args()

    try:
        credential = resolve_admin_credential(os.environ)
    except AdminCredentialError as exc:
        sys.exit(str(exc))
    print(f"admin auth: {credential.describe()}")
    if credential.shadowed:
        print(f"    note: {' + '.join(credential.shadowed)} also set — ignored")

    kc = args.kc.rstrip("/")
    base = f"{kc}/admin/realms/{args.realm}"

    def fresh_token() -> str:
        # Admin tokens default to 300s; refresh on each section so a
        # long run does not stall halfway through.
        return get_admin_token(kc, credential)

    scopes_to_create = [
        {
            "name": args.scope_read,
            "description": (
                "Read AKB documents, tables, files, and search across vaults "
                "you are a member of"
            ),
            "consent": "Read your AKB vaults (documents, tables, files, search)",
        },
        {
            "name": args.scope_write,
            "description": (
                "Create, edit, delete, and publish AKB content in vaults "
                "where you have writer/admin role"
            ),
            "consent": "Create, edit, and delete AKB content",
        },
    ]

    # ── 1. trusted-hosts ──────────────────────────────────────
    token = fresh_token()
    print("\n[1] trusted-hosts policy")
    status, comps, _ = http(
        "GET",
        f"{base}/components?type=org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy",
        token,
    )
    if status != 200 or not isinstance(comps, list):
        sys.exit(f"failed to list registration policies: {status} {comps}")
    th = next((c for c in comps if c.get("providerId") == "trusted-hosts"), None)
    if not th:
        sys.exit("trusted-hosts policy missing from realm (unexpected — Keycloak ships it by default)")
    current = set(th.get("config", {}).get("trusted-hosts", []) or [])
    print(f"    current: {sorted(current)}")
    desired = current | set(args.trusted_host)
    changed = False
    if desired != current:
        th["config"]["trusted-hosts"] = sorted(desired)
        changed = True
    # The sender-host check rejects every legitimate DCR from a moving
    # client (Claude Code on a laptop, claude.ai's egress, etc.) because
    # those hosts can't be allowlisted upfront. The redirect-URI check
    # below is the meaningful guard; turn the sender-host check off so
    # DCR actually works from anywhere a client lives.
    if th.get("config", {}).get("host-sending-registration-request-must-match") != ["false"]:
        th["config"]["host-sending-registration-request-must-match"] = ["false"]
        changed = True
    if changed:
        s, r, _ = http("PUT", f"{base}/components/{th['id']}", token, body=th)
        if s not in (200, 204):
            sys.exit(f"PUT trusted-hosts failed: {s} {r}")
        print(f"    updated: trusted-hosts={th['config']['trusted-hosts']} sender-check=off")
    else:
        print("    no-op (already permissive on sender + contains requested hosts)")

    # ── 1b. allowed-client-templates ──────────────────────────
    # The default "Allowed Client Scopes" policy rejects any DCR body
    # that includes `scope=openid`, because Keycloak does not list
    # `openid` in the realm's client-scope catalog (it is the OIDC
    # sentinel, not a Keycloak scope). MCP-spec clients (Claude Code,
    # claude.ai, ChatGPT) always send `openid` in the DCR scope field,
    # so the policy is incompatible with spec-compliant DCR and no
    # setting of it helps — the value it would have to allow cannot be
    # added. The `consent-required`, `trusted-hosts` (URI), and
    # `max-clients` policies remain as the meaningful guards.
    #
    # BOTH subtypes go, and the [authenticated] one is the less obvious
    # half. An Initial Access Token does not bypass registration
    # policies, it switches which subtype applies — so Protected DCR,
    # the option the design offers for hostile internet exposure, runs
    # into this same wall. Measured on a realm this script had already
    # configured: an IAT registration carrying the scope field returned
    # 403 "Policy 'Allowed Client Scopes' rejected request ... Not
    # permitted to use specified clientScope", and returned 201 only
    # with the scope field omitted. Leaving the authenticated policy in
    # place meant the open path worked and the hardened path did not,
    # which is backwards. The IAT itself is the gate on that path.
    print("\n[1b] allowed-client-templates policies")
    policies = client_scope_policies(comps)
    if policies:
        for policy in policies:
            subtype = policy.get("subType") or "unknown"
            s, r, _ = http("DELETE", f"{base}/components/{policy['id']}", token)
            if s not in (200, 204):
                sys.exit(f"DELETE allowed-client-templates [{subtype}] failed: {s} {r}")
            print(f"    removed [{subtype}] (was rejecting DCR bodies that include scope=openid)")
    else:
        print("    no-op (already removed)")

    # ── 2. client scopes + audience mappers ───────────────────
    created_ids: dict[str, str] = {}
    for sp in scopes_to_create:
        token = fresh_token()
        print(f"\n[2] client scope '{sp['name']}'")
        s, scopes, _ = http("GET", f"{base}/client-scopes", token)
        if not isinstance(scopes, list):
            sys.exit(f"failed to list scopes: {s} {scopes}")
        existing = next((x for x in scopes if x.get("name") == sp["name"]), None)
        if existing:
            print(f"    already exists id={existing['id']}")
            scope_id = existing["id"]
        else:
            payload = {
                "name": sp["name"],
                "description": sp["description"],
                "protocol": "openid-connect",
                "attributes": {
                    "consent.screen.text": sp["consent"],
                    "display.on.consent.screen": "true",
                    "include.in.token.scope": "true",
                },
            }
            s, r, headers = http("POST", f"{base}/client-scopes", token, body=payload)
            if s not in (201, 204):
                sys.exit(f"create scope failed: {s} {r}")
            loc = headers.get("Location", "")
            scope_id = loc.rstrip("/").split("/")[-1]
            print(f"    created id={scope_id}")
        created_ids[sp["name"]] = scope_id

        # Audience mapper attached to the scope.
        s, mappers, _ = http(
            "GET", f"{base}/client-scopes/{scope_id}/protocol-mappers/models", token,
        )
        if not isinstance(mappers, list):
            sys.exit(f"failed to read mappers: {s} {mappers}")
        aud = next(
            (m for m in mappers if m.get("protocolMapper") == "oidc-audience-mapper"),
            None,
        )
        if aud:
            current_aud = aud.get("config", {}).get("included.custom.audience")
            if current_aud != args.audience:
                aud["config"]["included.custom.audience"] = args.audience
                aud["config"]["id.token.claim"] = "false"
                aud["config"]["access.token.claim"] = "true"
                s, r, _ = http(
                    "PUT",
                    f"{base}/client-scopes/{scope_id}/protocol-mappers/models/{aud['id']}",
                    token, body=aud,
                )
                if s not in (200, 204):
                    sys.exit(f"PUT audience mapper failed: {s} {r}")
                print(f"    updated audience → {args.audience}")
            else:
                print(f"    audience mapper already → {args.audience}")
        else:
            mapper = {
                "name": "akb-mcp-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "consentRequired": False,
                "config": {
                    "included.custom.audience": args.audience,
                    "id.token.claim": "false",
                    "access.token.claim": "true",
                },
            }
            s, r, _ = http(
                "POST",
                f"{base}/client-scopes/{scope_id}/protocol-mappers/models",
                token, body=mapper,
            )
            if s not in (201, 204):
                sys.exit(f"create audience mapper failed: {s} {r}")
            print(f"    created audience mapper → {args.audience}")

    # ── 3. defaultOptionalClientScopes ────────────────────────
    token = fresh_token()
    print("\n[3] realm defaultOptionalClientScopes")
    s, current_opt, _ = http("GET", f"{base}/default-optional-client-scopes", token)
    if not isinstance(current_opt, list):
        sys.exit(f"failed to read optional scopes: {s} {current_opt}")
    have = {x["name"] for x in current_opt}
    for name, sid in created_ids.items():
        if name in have:
            print(f"    {name} already optional — skip")
            continue
        s, r, _ = http(
            "PUT", f"{base}/default-optional-client-scopes/{sid}",
            token, body={},
        )
        if s not in (200, 204):
            sys.exit(f"PUT default-optional-client-scope failed: {s} {r}")
        print(f"    added {name}")

    # ── 4. Verify ─────────────────────────────────────────────
    token = fresh_token()
    print("\n[verify]")
    s, comps, _ = http(
        "GET",
        f"{base}/components?type=org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy",
        token,
    )
    # Every read below is `object` as far as the type system knows, because
    # `http` cannot promise a shape. Narrow each one instead of indexing on
    # faith: an error body here used to reach `for c in comps` as a
    # TypeError, and the missing-policy case reached `next()` as a bare
    # StopIteration — two ways for a verification step to fail as something
    # other than "verification failed".
    if not isinstance(comps, list):
        sys.exit(f"verify: could not list registration policies: {s} {comps}")
    th2 = next(
        (c for c in comps if isinstance(c, dict) and c.get("providerId") == "trusted-hosts"),
        None,
    )
    print(f"    trusted-hosts: {th2['config'].get('trusted-hosts') if th2 else 'MISSING'}")
    print(f"    allowed-client-templates: {sorted(_policy_subtypes(comps)) or 'removed'}")
    s, scopes, _ = http("GET", f"{base}/client-scopes", token)
    if not isinstance(scopes, list):
        sys.exit(f"verify: could not list client scopes: {s} {scopes}")
    by_name = {x["name"]: x for x in scopes if isinstance(x, dict)}
    for sp in scopes_to_create:
        x = by_name.get(sp["name"])
        if not x:
            print(f"    {sp['name']}: MISSING"); continue
        s2, m, _ = http(
            "GET", f"{base}/client-scopes/{x['id']}/protocol-mappers/models", token,
        )
        a = None
        if isinstance(m, list):
            a = next(
                (mm for mm in m
                 if isinstance(mm, dict) and mm.get("protocolMapper") == "oidc-audience-mapper"),
                None,
            )
        aud = a["config"].get("included.custom.audience") if a else "MISSING"
        print(f"    {sp['name']}: id={x['id'][:8]}.. aud={aud}")
    s, opt, _ = http("GET", f"{base}/default-optional-client-scopes", token)
    if not isinstance(opt, list):
        sys.exit(f"verify: could not read optional scopes: {s} {opt}")
    print(f"    defaultOptionalClientScopes: {sorted(x['name'] for x in opt if isinstance(x, dict))}")
    print("\nDONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
