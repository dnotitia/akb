"""HOST-only compensation of exact unchanged operation-created target users.

Requires a newly issued one-time master client in a protected host file. Never
reactivates preparation authority, deletes a pre-existing user, or queries AKB
storage. The caller supplies the original source guard for platform readback.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.services.standalone_sso_bootstrap import StandaloneSSOBootstrapError

from app.sso.adopt_prepared_target import PreparedTargetReceipt
from app.sso.prepare_target_host import (ExistingTargetControl, HostPreparationInput, canonical, digest, fail,
                                         protected_read, protected_write)


class CleanupAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    target_id: UUID
    realm_id: UUID
    client_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,255}$")
    client_secret: SecretStr


def ids_hash(ids) -> str:
    return digest(json.dumps(sorted(str(value) for value in ids), separators=(",", ":"), ensure_ascii=True).encode())


async def cleanup_target(preparation: HostPreparationInput, receipt: PreparedTargetReceipt,
                          authority: CleanupAuthority, output_file: Path) -> dict[str, object]:
    if (receipt.target_id != preparation.target_id or receipt.operation_id != preparation.operation_id
            or receipt.org_id != preparation.org_id or receipt.realm_id != preparation.realm_id
            or receipt.issuer != preparation.issuer or receipt.source_guard != preparation.source_guard
            or receipt.source_guard is None
            or authority.target_id != receipt.target_id or authority.operation_id != receipt.operation_id
            or authority.realm_id != receipt.realm_id or authority.client_id == preparation.bootstrap_client_id
            or not receipt.authority_retired_at):
        raise fail("target_cleanup_binding_mismatch")
    expected_akb_ids = {m.akb_user_id for m in preparation.cohort}
    if expected_akb_ids != {m.akb_user_id for m in receipt.cohort}:
        raise fail("target_cleanup_cohort_mismatch")
    if output_file.parent.is_symlink() or output_file.parent.stat().st_mode & 0o077:
        raise fail("target_cleanup_output_directory_invalid")
    lockfd = os.open(output_file.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        os.close(lockfd)
        raise fail("target_cleanup_already_running") from None
    control = ExistingTargetControl(preparation)
    spec = replace(preparation.spec(), bootstrap_client_id=authority.client_id,
                   bootstrap_client_secret=authority.client_secret.get_secret_value())
    journal_path = output_file.with_suffix(".host-journal.json")
    binding = digest(canonical(receipt.model_dump(mode="json")))
    try:
        journal = json.loads(protected_read(journal_path)) if journal_path.exists() else None
        if journal is not None and journal.get("binding") != binding:
            raise fail("target_cleanup_journal_mismatch")
        token = await control.acquire_bootstrap(spec)
        if token is None and journal is None:
            published = json.loads(protected_read(output_file))
            if published.get("preparation_sha256") != binding:
                raise fail("target_cleanup_receipt_mismatch")
            return published
        if token is None and (journal is None or journal["phase"] != "retiring"):
            raise fail("target_cleanup_authority_lost")
        if journal is None:
            journal = {"binding": binding, "phase": "cleaning", "token": token, "deleted": [], "pending": None}
            protected_write(journal_path, journal)
        if journal["phase"] == "cleaning":
            if token is None:
                raise fail("target_cleanup_authority_lost")
            await control.require_realm(spec, token=token)
            actuals = []
            # Preflight every owned object before any deletion. Its full
            # selected state, credential list, federation and roles must match.
            for asset in receipt.owned_assets:
                subject = str(UUID(str(asset["subject"])))
                actual = await control.user_fingerprint(spec, subject, token=token)
                if actual is None:
                    if subject not in journal["deleted"] and journal["pending"] != subject:
                        raise fail("target_cleanup_owned_user_missing")
                elif actual != asset or not actual["owned"]:
                    raise fail("target_cleanup_owned_user_changed")
                actuals.append((subject, actual))
            for (subject, actual), asset in zip(actuals, receipt.owned_assets, strict=True):
                if actual is not None:
                    journal["pending"] = subject
                    protected_write(journal_path, journal)
                    # Other users' preflight reads and previous deletes yield.
                    # Recheck this exact object immediately before its DELETE.
                    # Keycloak offers no conditional user DELETE: this narrows
                    # the race; full realm administrators can still bypass it.
                    fresh = await control.user_fingerprint(spec, subject, token=token)
                    if fresh != asset or not fresh["owned"]:
                        raise fail("target_cleanup_owned_user_changed")
                    try:
                        await control._request(spec, "DELETE", f"/admin/realms/{spec.realm}/users/{subject}",
                                               token=token, expected=frozenset({204}))
                    except Exception:
                        if await control.user_fingerprint(spec, subject, token=token) is not None:
                            raise
                if subject not in journal["deleted"]:
                    journal["deleted"].append(subject)
                journal["pending"] = None
                protected_write(journal_path, journal)
            remaining = []
            for asset in receipt.owned_assets:
                if await control.user_fingerprint(spec, str(asset["subject"]), token=token) is not None:
                    remaining.append(asset["subject"])
            existing = []
            for subject in receipt.preserved_broker_ids:
                actual = await control.user_fingerprint(spec, subject, token=token)
                if actual is None:
                    raise fail("target_cleanup_preexisting_user_missing")
                existing.append(subject)
            journal.update(phase="retiring", remaining=remaining, existing=existing,
                           readback_sha256=digest(canonical({"remaining": remaining, "existing": existing})),
                           compensated_at=datetime.now(timezone.utc).isoformat())
            protected_write(journal_path, journal)
        if token is not None:
            try:
                await control.retire_bootstrap(spec, bootstrap_token=token)
            except Exception:
                await control.assert_bootstrap_retired(spec, bootstrap_token=journal["token"])
        await control.assert_bootstrap_retired(spec, bootstrap_token=journal["token"])
        result = {"schema_version": 1, "operation_id": str(receipt.operation_id), "org_id": str(receipt.org_id),
                  "target_id": str(receipt.target_id), "realm_id": str(receipt.realm_id), "issuer": receipt.issuer,
                  "config_sha256": receipt.config_sha256, "preparation_sha256": binding,
                  "source_guard": receipt.source_guard.model_dump(mode="json"),
                  "compensated_at": journal["compensated_at"], "authority_retired_at": datetime.now(timezone.utc).isoformat(),
                  "deleted_owned_ids": sorted(journal["deleted"]), "remaining_owned_ids": journal["remaining"],
                  "preserved_existing_ids_sha256": ids_hash(expected_akb_ids),
                  "preserved_existing_broker_ids_sha256": ids_hash(journal["existing"]),
                  "actual_readback_sha256": journal["readback_sha256"]}
        protected_write(output_file, result)
        journal_path.unlink()
        return result
    finally:
        await control.aclose()
        fcntl.flock(lockfd, fcntl.LOCK_UN)
        os.close(lockfd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", type=Path, required=True)
    parser.add_argument("--preparation-file", type=Path, required=True)
    parser.add_argument("--authority-file", type=Path, required=True)
    parser.add_argument("--output-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        preparation = HostPreparationInput.model_validate_json(protected_read(args.input_file))
        receipt = PreparedTargetReceipt.model_validate_json(protected_read(args.preparation_file))
        authority = CleanupAuthority.model_validate_json(protected_read(args.authority_file))
        result = asyncio.run(cleanup_target(preparation, receipt, authority, args.output_file))
        remaining = result["remaining_owned_ids"]
        if not isinstance(remaining, list):
            raise fail("target_cleanup_readback_invalid")
        print(json.dumps({"compensated": True, "target_id": result["target_id"],
                          "remaining_owned_count": len(remaining)}))
        return 0
    except StandaloneSSOBootstrapError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    except Exception:
        print("target_cleanup_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
