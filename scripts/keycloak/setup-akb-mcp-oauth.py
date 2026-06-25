#!/usr/bin/env python3
"""Apply MCP OAuth Resource Server changes to a Keycloak realm.

Idempotent — running this twice is a no-op on the second call. Survives
partial state from a prior interrupted run.

What it does (in order):

1. Add ``localhost`` / ``127.0.0.1`` to the realm's DCR ``trusted-hosts``
   policy so a local Claude Code (or another DCR-capable MCP client
   running on the operator's laptop) can register itself dynamically.
2. Create the ``akb:vault:read`` client scope (if absent) with an
   ``oidc-audience-mapper`` whose ``included.custom.audience`` is the
   AKB ``/mcp`` URL the realm should mint tokens for.
3. Create the ``akb:vault:write`` client scope the same way.
4. Add both scopes to ``defaultOptionalClientScopes`` so a DCR-registered
   public client can request them at the authorize endpoint.
5. Verify everything by re-reading state.

Reads admin credentials from ``KC_ADMIN_USER`` / ``KC_ADMIN_PASS`` env
vars; never accepts them on the command line so they cannot land in
shell history or process listings.

Usage:
    KC_ADMIN_USER=admin KC_ADMIN_PASS=... \\
        python3 scripts/keycloak/setup-akb-mcp-oauth.py \\
            --kc https://auth.example.com \\
            --realm akb \\
            --audience https://akb.example.com/mcp

See docs/designs/mcp-oauth-dcr/00-overview.md for the rationale and
docs/mcp-clients/web-connectors.md for the end-to-end client walkthrough.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


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


def get_admin_token(kc_url: str, user: str, password: str) -> str:
    status, payload, _ = http(
        "POST",
        f"{kc_url}/realms/master/protocol/openid-connect/token",
        body={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": user,
            "password": password,
        },
        ctype="application/x-www-form-urlencoded",
    )
    if status != 200 or not isinstance(payload, dict):
        sys.exit(f"admin auth failed: {status} {payload}")
    tok = payload.get("access_token")
    if not tok:
        sys.exit(f"admin auth: no access_token in {payload}")
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

    user = os.environ.get("KC_ADMIN_USER")
    pwd = os.environ.get("KC_ADMIN_PASS")
    if not user or not pwd:
        sys.exit(
            "KC_ADMIN_USER and KC_ADMIN_PASS env vars are required. "
            "Set them temporarily for this run; the script never persists them."
        )

    kc = args.kc.rstrip("/")
    base = f"{kc}/admin/realms/{args.realm}"

    def fresh_token() -> str:
        # Admin tokens default to 300s; refresh on each section so a
        # long run does not stall halfway through.
        return get_admin_token(kc, user, pwd)

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
    # The default "Allowed Client Scopes" anonymous-DCR policy rejects
    # any DCR body that includes `scope=openid` because Keycloak does
    # not list `openid` in the realm's client-scope catalog (it is the
    # OIDC sentinel, not a Keycloak scope). MCP-spec clients (Claude
    # Code, claude.ai, ChatGPT) always send `openid` in the DCR scope
    # field, so this policy must be removed for anonymous DCR. The
    # `consent-required`, `trusted-hosts` (URI), and `max-clients`
    # policies remain as the meaningful guards. The [authenticated]
    # variant of this policy stays — it gates registrations made with
    # an Initial Access Token, which is the operator-controlled path.
    print("\n[1b] allowed-client-templates policy (anonymous)")
    actp = next(
        (c for c in comps if c.get("providerId") == "allowed-client-templates"
         and c.get("subType") == "anonymous"),
        None,
    )
    if actp:
        s, r, _ = http("DELETE", f"{base}/components/{actp['id']}", token)
        if s not in (200, 204):
            sys.exit(f"DELETE allowed-client-templates failed: {s} {r}")
        print("    removed (was rejecting DCR bodies that include scope=openid)")
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
    th2 = next(c for c in comps if c.get("providerId") == "trusted-hosts")
    print(f"    trusted-hosts: {th2['config'].get('trusted-hosts')}")
    s, scopes, _ = http("GET", f"{base}/client-scopes", token)
    by_name = {x["name"]: x for x in scopes if isinstance(x, dict)}
    for sp in scopes_to_create:
        x = by_name.get(sp["name"])
        if not x:
            print(f"    {sp['name']}: MISSING"); continue
        s2, m, _ = http(
            "GET", f"{base}/client-scopes/{x['id']}/protocol-mappers/models", token,
        )
        a = next(
            (mm for mm in m if mm.get("protocolMapper") == "oidc-audience-mapper"),
            None,
        )
        print(f"    {sp['name']}: id={x['id'][:8]}.. aud={a['config'].get('included.custom.audience') if a else 'MISSING'}")
    s, opt, _ = http("GET", f"{base}/default-optional-client-scopes", token)
    print(f"    defaultOptionalClientScopes: {sorted(x['name'] for x in opt)}")
    print("\nDONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
