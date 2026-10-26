"""Image-owned semantic verification of an actual bootstrap CLI receipt.

Run only against the installation whose protected intent/receipt were produced
by the CLI. This does not seed a PAT, manufacture readiness or mint through an
HTTP issuer exception. Failure prints only a stable code.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.db.postgres import close_pool
from app.services import auth_service
from app.services.platform_service_bootstrap import (
    BootstrapRefused, CAPABILITY, provision, read_intent, write_receipt,
)
from app.services.token_issuer_policy import require_issuer_carrier
from app.exceptions import AKBError


async def probe(intent_file: str, receipt_file: str) -> dict:
    intent = read_intent(intent_file)
    # The receipt is secret-free. Bound its bytes even on an operator-supplied path.
    with open(receipt_file, "rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise BootstrapRefused("platform_bootstrap_probe_receipt")
    receipt = json.loads(raw)
    actual = await provision(intent, replay_only=True)
    if actual["status"] != "replayed" or not isinstance(receipt, dict) or receipt.get("status") not in ("issued", "replayed"):
        raise BootstrapRefused("platform_bootstrap_probe_not_prepared")
    if {**receipt, "status": "replayed"} != actual:
        raise BootstrapRefused("platform_bootstrap_probe_receipt_mismatch")
    principal = await auth_service.resolve_rest_user_authorization("Bearer " + intent.credential)
    if (principal is None or principal.user_id != actual["user_id"] or principal.username != "platform-bot"
            or principal.email != actual["email"] or not principal.is_admin or principal.account_kind != "service"
            or principal.auth_method != "pat" or principal.key_class != "service" or principal.vault_scope is not None
            or principal.token_id != actual["token_id"] or principal.token_scopes != frozenset(("read", "write", "admin"))):
        raise BootstrapRefused("platform_bootstrap_probe_authentication")
    # Same installed policy used by generic HTTP mint. The dedicated CLI must
    # never turn management OAuth into a generic issuer capability.
    oauth = auth_service.AuthenticatedUser(user_id=principal.user_id, username=principal.username,
        email=principal.email, display_name=principal.display_name, is_admin=True, auth_method="oauth", account_kind="service")
    try:
        require_issuer_carrier(oauth, admin_route=True)
    except AKBError as exc:
        if exc.code != "human_token_issuer_required":
            raise BootstrapRefused("platform_bootstrap_probe_issuer_guard") from None
    else:
        raise BootstrapRefused("platform_bootstrap_probe_issuer_guard")
    return {"schema_version": 1, "capability": CAPABILITY, "status": "verified",
            "operation_id": actual["operation_id"], "user_id": actual["user_id"], "token_id": actual["token_id"],
            "intent_sha256": actual["intent_sha256"], "credential_sha256": actual["credential_sha256"],
            "runtime_identity": actual["runtime_identity"]}


async def _run(args) -> int:
    result = None
    failed = False
    try:
        result = await probe(args.intent_file, args.receipt_file)
    except Exception:
        failed = True
    finally:
        try:
            await close_pool()
        except Exception:
            failed = True
    if not failed and result is not None:
        try:
            write_receipt(args.output_file, result)
            return 0
        except Exception:
            failed = True
    print("platform_bootstrap_probe_failed", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent-file", required=True)
    parser.add_argument("--receipt-file", required=True)
    parser.add_argument("--output-file", required=True)
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
