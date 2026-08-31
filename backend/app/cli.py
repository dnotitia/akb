"""AKB management CLI.

Invoke via:
    docker compose exec backend python -m app.cli <subcommand> [args]
or, on a server with the backend installed:
    python -m app.cli <subcommand> [args]

The backend container is pip-installed (no uv inside). Use plain `python`
in all in-container invocations.

Subcommands:
    generate-local-session-keyset
                               Create a non-overwriting RSA-3072 private key
                               and public JWKS for local-session-rs256-v2.
    provision-recovery-admin   Explicitly provision the designated local or
                               SSO recovery administrator. Password material
                               is accepted only by file/stdin and is never
                               printed.
    issue-recovery-admin-credential
                               Break-glass: replace the exact designated
                               recovery administrator's credential and print
                               the new one once. The credential it replaces
                               stops working. Nothing stores or logs the value.
    bootstrap-standalone-sso   Converge the bundled Keycloak realm, clients,
                               signing profile, and exact AKB recovery-admin
                               projection; then retire the temporary bootstrap
                               service account.
    reset-password <username>   Generate a temp password for the given user.
                                 Prints the temp password to stdout. Caller
                                 must share it with the user out-of-band.
    repair-resource-hashes      Backfill document/file content-hash projections.
    initialize-postgres-native  Claim and initialize a never-used PostgreSQL
                                database for the stable Native backend.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import NoReturn


REPAIR_RESOURCE_HASHES_USAGE = (
    "Usage: python -m app.cli repair-resource-hashes [--vault NAME] [--documents-only|--files-only] [--limit N]"
)

PROVISION_RECOVERY_ADMIN_USAGE = (
    "Usage: python -m app.cli provision-recovery-admin "
    "{local|sso} --username USER --email EMAIL [profile options]\n"
    "  local: (--password-file PATH|- | --generate-password-file PATH)\n"
    "  sso:   --issuer ISSUER --subject SUBJECT"
)

GENERATE_LOCAL_SESSION_KEYSET_USAGE = (
    "Usage: python -m app.cli generate-local-session-keyset --output-dir DIR [--retain-jwks PATH ...]"
)

ISSUE_RECOVERY_ADMIN_CREDENTIAL_USAGE = (
    "Usage: python -m app.cli issue-recovery-admin-credential --expected-username USER --expected-email EMAIL"
)

STANDALONE_SSO_BOOTSTRAP_USAGE = (
    "Usage: python -m app.cli bootstrap-standalone-sso "
    "--bootstrap-client-id ID --bootstrap-client-secret-file PATH "
    "[--upgrade-client-id ID --upgrade-client-secret-file PATH] "
    "--product-admin-username USER --product-admin-email EMAIL "
    "--product-admin-password-file PATH"
)

MIGRATE_REVISION_BACKEND_USAGE = (
    "Usage: python -m app.cli migrate-revision-backend "
    "{plan --coverage-version VERSION|apply|verify|commit|abort --cutover-id UUID|"
    "retire-external-git --vault-id UUID --manifest-file PATH --idempotency-key UUID "
    "--requested-by ID --confirm-planned-downtime RETIRE-EXTERNAL-GIT:UUID}"
)


class _CLIUsageError(Exception):
    pass


class _ProvisioningInputError(Exception):
    pass


async def _initialize_operator_database() -> None:
    """Initialize schema and the user-role projection needed by admin CLIs."""
    from app.db.postgres import get_pool, init_db
    from app.services.role_sync import RoleSync, set_role_sync

    await init_db()
    set_role_sync(RoleSync(await get_pool()))


def _generate_local_session_keyset(args: list[str]) -> int:
    parser = _SafeArgumentParser(add_help=False)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--retain-jwks", action="append", default=[])
    try:
        parsed = parser.parse_args(args)
    except _CLIUsageError:
        print(GENERATE_LOCAL_SESSION_KEYSET_USAGE, file=sys.stderr)
        return 2

    from app.services.local_session_keys import (
        LocalSessionKeyConfigurationError,
        generate_local_session_keyset,
    )

    try:
        report = generate_local_session_keyset(
            parsed.output_dir,
            retain_jwks_paths=parsed.retain_jwks,
        )
    except LocalSessionKeyConfigurationError as exc:
        print(f"local_session_keyset_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


class _SafeArgumentParser(argparse.ArgumentParser):
    """Raise a value-free error so an accidental secret argument is not echoed."""

    def error(self, _message: str) -> NoReturn:
        raise _CLIUsageError()


def _recovery_admin_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(add_help=False)
    profiles = parser.add_subparsers(dest="profile", required=True)

    local = profiles.add_parser("local", add_help=False)
    local.add_argument("--username", required=True)
    local.add_argument("--email", required=True)
    password_source = local.add_mutually_exclusive_group(required=True)
    password_source.add_argument("--password-file")
    password_source.add_argument("--generate-password-file")

    sso = profiles.add_parser("sso", add_help=False)
    sso.add_argument("--username", required=True)
    sso.add_argument("--email", required=True)
    sso.add_argument("--issuer", required=True)
    sso.add_argument("--subject", required=True)
    return parser


def _read_password_source(source: str) -> str:
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError:
        raise _ProvisioningInputError("Unable to read the password source") from None
    password = text.rstrip("\r\n")
    if not password:
        raise _ProvisioningInputError("The password source is empty")
    if "\r" in password or "\n" in password:
        raise _ProvisioningInputError("The password source must contain one line")
    return password


def _read_optional_bootstrap_secret_file(source: str) -> str:
    if source == "-":
        raise _ProvisioningInputError("Standalone SSO secrets require explicit files")
    try:
        text = Path(source).read_text(encoding="utf-8")
    except FileNotFoundError:
        # The default overlay keeps required Secret objects with retired
        # sentinel values.  A custom steady-state overlay may unmount them;
        # receipt-backed readback then proves the permanent management path.
        return ""
    except OSError:
        raise _ProvisioningInputError("Unable to read the bootstrap secret source") from None
    secret = text.rstrip("\r\n")
    if "\r" in secret or "\n" in secret:
        raise _ProvisioningInputError("A bootstrap secret source must contain one line")
    return secret


def _write_generated_password(target: str, password: str) -> Path:
    if target == "-":
        raise _ProvisioningInputError("Generated passwords require an explicit file")
    path = Path(target)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    fd: int | None = None
    try:
        fd = os.open(path, flags, 0o600)
        created = True
        os.fchmod(fd, 0o600)
        output = os.fdopen(fd, "w", encoding="utf-8")
        fd = None  # ownership transferred to the file object
        with output:
            output.write(password)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except OSError, ValueError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise _ProvisioningInputError("Unable to create the generated-password file") from None
    return path


def _remove_generated_password(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


async def _provision_recovery_admin(args: list[str]) -> int:
    try:
        parsed = _recovery_admin_parser().parse_args(args)
    except _CLIUsageError:
        print(PROVISION_RECOVERY_ADMIN_USAGE, file=sys.stderr)
        return 2

    from app.config import AuthModeConfigurationError
    from app.db.postgres import close_pool
    from app.exceptions import AKBError
    from app.services.recovery_admin_service import (
        provision_local_recovery_admin,
        provision_sso_recovery_admin,
    )

    generated_path: Path | None = None
    keep_generated = False
    report: dict | None = None
    error: tuple[str, str] | None = None
    try:
        await _initialize_operator_database()
        if parsed.profile == "local":
            if parsed.generate_password_file is not None:
                password = secrets.token_urlsafe(32)
                generated_path = _write_generated_password(
                    parsed.generate_password_file,
                    password,
                )
            else:
                password = _read_password_source(parsed.password_file)
            report = await provision_local_recovery_admin(
                username=parsed.username,
                email=parsed.email,
                password=password,
                # True for exactly the branch above that produced the value
                # and wrote it into an operator-owned file: that is AKB
                # handing a credential to a person, so the account it creates
                # owes a replacement for it. A caller-supplied password was
                # never handed over by AKB.
                credential_issued_by_akb=generated_path is not None,
            )
        else:
            report = await provision_sso_recovery_admin(
                username=parsed.username,
                email=parsed.email,
                issuer=parsed.issuer,
                subject=parsed.subject,
            )
        keep_generated = generated_path is not None and bool(report["created"])
    except AKBError as exc:
        error = (exc.code or "recovery_admin_provisioning_failed", exc.message)
    except AuthModeConfigurationError as exc:
        error = ("recovery_admin_mode_configuration", str(exc))
    except _ProvisioningInputError as exc:
        error = ("recovery_admin_secret_source", str(exc))
    except Exception:
        # Never render an unexpected exception: driver/library text can include
        # arguments, and this command handles one-time credential material.
        error = (
            "recovery_admin_provisioning_failed",
            "Recovery administrator provisioning failed",
        )
    finally:
        if generated_path is not None and not keep_generated:
            if not _remove_generated_password(generated_path):
                error = (
                    "recovery_admin_secret_cleanup_failed",
                    "Generated-password file cleanup failed",
                )
        try:
            await close_pool()
        except Exception:
            if error is None:
                error = (
                    "recovery_admin_database_cleanup_failed",
                    "Database connection cleanup failed",
                )

    if error is not None:
        print(f"{error[0]}: {error[1]}", file=sys.stderr)
        return 1
    assert report is not None
    public_report = {
        "user_id": report["user_id"],
        "username": report["username"],
        "email": report["email"],
        "auth_mode": report["auth_mode"],
        "created": report["created"],
        "is_admin": report["is_admin"],
        "is_recovery_admin": report["is_recovery_admin"],
        "password_file_written": keep_generated,
    }
    print(json.dumps(public_report, sort_keys=True))
    return 0


async def _issue_recovery_admin_credential(args: list[str]) -> int:
    parser = _SafeArgumentParser(add_help=False)
    parser.add_argument("--expected-username", required=True)
    parser.add_argument("--expected-email", required=True)
    try:
        parsed = parser.parse_args(args)
    except _CLIUsageError:
        print(ISSUE_RECOVERY_ADMIN_CREDENTIAL_USAGE, file=sys.stderr)
        return 2

    from app.config import AuthModeConfigurationError
    from app.db.postgres import close_pool
    from app.exceptions import AKBError
    from app.services.recovery_admin_service import issue_recovery_admin_credential

    report: dict | None = None
    error: tuple[str, str] | None = None
    try:
        await _initialize_operator_database()
        # Same service function as the endpoint. Workspace shell access is the
        # authority here, so there is no authenticated principal to pass. This
        # replaces a credential the account already has; it is not a way in for
        # an account that never had one.
        report = await issue_recovery_admin_credential(
            expected_username=parsed.expected_username,
            expected_email=parsed.expected_email,
            method="recovery_admin_cli",
        )
    except AKBError as exc:
        error = (exc.code or "recovery_admin_credential_issue_failed", exc.message)
    except AuthModeConfigurationError as exc:
        error = ("recovery_admin_credential_mode_configuration", str(exc))
    except Exception:
        # Never render an unexpected exception: driver and HTTP libraries can
        # retain request bodies, and this command handles credential material.
        error = (
            "recovery_admin_credential_issue_failed",
            "Recovery administrator credential issue failed",
        )
    finally:
        try:
            await close_pool()
        except Exception:
            if error is None:
                error = (
                    "recovery_admin_credential_database_cleanup_failed",
                    "Database connection cleanup failed",
                )

    if error is not None:
        print(f"{error[0]}: {error[1]}", file=sys.stderr)
        return 1
    assert report is not None
    # The one-time reveal of the replacement. It is deliberately not part of
    # the JSON report so a caller piping stdout into a log or a file captures
    # the identity, not the credential.
    print(
        json.dumps(
            {
                "user_id": report["user_id"],
                "username": report["username"],
                "email": report["email"],
                "auth_mode": report["auth_mode"],
            },
            sort_keys=True,
        )
    )
    print(f"Credential for {report['username']}: {report['credential']}")
    print("Share this out-of-band. It cannot be retrieved again.")
    return 0


def _standalone_sso_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(add_help=False)
    parser.add_argument("--bootstrap-client-id", required=True)
    parser.add_argument("--bootstrap-client-secret-file", required=True)
    parser.add_argument(
        "--upgrade-client-id",
        default="akb-bootstrap-upgrade-v2",
    )
    parser.add_argument("--upgrade-client-secret-file")
    parser.add_argument("--product-admin-username", required=True)
    parser.add_argument("--product-admin-email", required=True)
    parser.add_argument("--product-admin-password-file", required=True)
    return parser


async def _bootstrap_standalone_sso(args: list[str]) -> int:
    try:
        parsed = _standalone_sso_parser().parse_args(args)
    except _CLIUsageError:
        print(STANDALONE_SSO_BOOTSTRAP_USAGE, file=sys.stderr)
        return 2

    from app.config import AuthModeConfigurationError, settings
    from app.db.postgres import close_pool
    from app.services.recovery_admin_service import provision_sso_recovery_admin
    from app.services.standalone_sso_bootstrap import (
        StandaloneSSOBootstrapError,
        StandaloneSSOBootstrapSpec,
        bootstrap_standalone_sso,
    )
    from app.services.standalone_sso_keycloak import KeycloakStandaloneSSOControl
    from app.services.standalone_sso_receipt import (
        load_standalone_sso_retirement_receipt,
        record_standalone_sso_retirement_receipt,
    )

    control: KeycloakStandaloneSSOControl | None = None
    report: dict[str, object] | None = None
    error: tuple[str, str] | None = None
    try:
        bootstrap_secret = _read_optional_bootstrap_secret_file(parsed.bootstrap_client_secret_file)
        upgrade_secret = (
            _read_optional_bootstrap_secret_file(parsed.upgrade_client_secret_file)
            if parsed.upgrade_client_secret_file is not None
            else ""
        )
        product_admin_password = _read_optional_bootstrap_secret_file(parsed.product_admin_password_file)
        if settings.require_auth_mode() != "sso" or not settings.keycloak_enabled:
            raise _ProvisioningInputError("Standalone SSO bootstrap requires auth_mode=sso and Keycloak enabled")
        required = {
            "keycloak_server_url": settings.keycloak_server_url,
            "keycloak_internal_url or keycloak_server_url": (
                settings.keycloak_internal_url or settings.keycloak_server_url
            ),
            "keycloak_realm": settings.keycloak_realm,
            "public_base_url": settings.public_base_url,
            "keycloak_client_id": settings.keycloak_client_id,
            "keycloak_client_secret": settings.keycloak_client_secret,
            "keycloak_admin_client_id": settings.keycloak_admin_client_id,
            "keycloak_admin_client_secret": settings.keycloak_admin_client_secret,
            "keycloak_management_client_id": settings.keycloak_management_client_id,
            "keycloak_management_client_secret": (settings.keycloak_management_client_secret),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise _ProvisioningInputError("Standalone SSO configuration is incomplete: " + ", ".join(missing))
        if settings.keycloak_public_client:
            raise _ProvisioningInputError("Bundled standalone SSO requires a confidential API client")
        credentials = [
            settings.keycloak_client_secret,
            settings.keycloak_admin_client_secret,
            settings.keycloak_management_client_secret,
            bootstrap_secret,
            upgrade_secret,
            product_admin_password,
        ]
        configured_credentials = [value for value in credentials if value]
        if len(set(configured_credentials)) != len(configured_credentials):
            raise _ProvisioningInputError("Standalone SSO credentials must be independently generated")
        client_ids = {
            settings.keycloak_client_id,
            settings.keycloak_admin_client_id,
            settings.keycloak_management_client_id,
            parsed.upgrade_client_id,
        }
        if len(client_ids) != 4:
            raise _ProvisioningInputError("Standalone SSO API, admin, management, and upgrade clients must be distinct")

        spec = StandaloneSSOBootstrapSpec(
            keycloak_internal_url=(settings.keycloak_internal_url or settings.keycloak_server_url),
            keycloak_public_url=settings.keycloak_server_url,
            realm=settings.keycloak_realm,
            akb_public_url=settings.public_base_url,
            bootstrap_client_id=parsed.bootstrap_client_id,
            bootstrap_client_secret=bootstrap_secret,
            management_client_id=settings.keycloak_management_client_id,
            management_client_secret=settings.keycloak_management_client_secret,
            api_client_id=settings.keycloak_client_id,
            api_client_secret=settings.keycloak_client_secret,
            admin_client_id=settings.keycloak_admin_client_id,
            admin_client_secret=settings.keycloak_admin_client_secret,
            product_admin_username=parsed.product_admin_username,
            product_admin_email=parsed.product_admin_email,
            product_admin_password=product_admin_password,
            backchannel_logout_uri=(settings.keycloak_backchannel_logout_uri_effective),
            upgrade_client_id=parsed.upgrade_client_id,
            upgrade_client_secret=upgrade_secret,
        )
        await _initialize_operator_database()
        control = KeycloakStandaloneSSOControl(verify_ssl=settings.keycloak_verify_ssl)
        report = await bootstrap_standalone_sso(
            spec,
            control=control,
            provision_admin=provision_sso_recovery_admin,
            load_retirement_receipt=load_standalone_sso_retirement_receipt,
            record_retirement_receipt=record_standalone_sso_retirement_receipt,
        )
    except StandaloneSSOBootstrapError as exc:
        error = (exc.code, "Standalone SSO bootstrap failed")
    except AuthModeConfigurationError as exc:
        error = ("standalone_sso_mode_configuration", str(exc))
    except _ProvisioningInputError as exc:
        error = ("standalone_sso_input_invalid", str(exc))
    except Exception:
        # Keycloak, HTTP, database, and driver exceptions may retain request
        # bodies or DSNs. Never render an unexpected exception from this
        # credential-bearing command.
        error = (
            "standalone_sso_bootstrap_failed",
            "Standalone SSO bootstrap failed",
        )
    finally:
        if control is not None:
            try:
                await control.aclose()
            except Exception:
                if error is None:
                    error = (
                        "standalone_sso_keycloak_cleanup_failed",
                        "Keycloak connection cleanup failed",
                    )
        try:
            await close_pool()
        except Exception:
            if error is None:
                error = (
                    "standalone_sso_database_cleanup_failed",
                    "Database connection cleanup failed",
                )

    if error is not None:
        print(f"{error[0]}: {error[1]}", file=sys.stderr)
        return 1
    assert report is not None
    print(json.dumps(report, sort_keys=True))
    return 0


async def _reset_password(username: str) -> int:
    from app.exceptions import AKBError, NotFoundError
    from app.services.password_service import reset_password

    try:
        temp, uname = await reset_password(
            username=username,
            actor_id=None,
            method="cli",
        )
    except NotFoundError:
        print(f"User not found: {username}", file=sys.stderr)
        return 1
    except AKBError as error:
        code = error.code or "reset_password_failed"
        print(f"{code}: {error.message}", file=sys.stderr)
        return 1
    print(f"Temporary password for {uname}: {temp}")
    print("Share this with the user out-of-band. It cannot be retrieved again.")
    return 0


async def _repair_resource_hashes(args: list[str]) -> int:
    from app.services.resource_integrity import repair_resource_hashes

    vault = None
    include_documents = True
    include_files = True
    limit = 100
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--vault":
            index += 1
            if index >= len(args):
                print(REPAIR_RESOURCE_HASHES_USAGE, file=sys.stderr)
                return 2
            vault = args[index]
        elif arg == "--documents-only":
            include_files = False
        elif arg == "--files-only":
            include_documents = False
        elif arg == "--limit":
            index += 1
            if index >= len(args):
                print(REPAIR_RESOURCE_HASHES_USAGE, file=sys.stderr)
                return 2
            try:
                limit = int(args[index])
            except ValueError:
                print("--limit must be an integer", file=sys.stderr)
                return 2
        else:
            print(f"Unknown repair-resource-hashes option: {arg}", file=sys.stderr)
            return 2
        index += 1

    if not include_documents and not include_files:
        print("Choose at least one resource kind to repair", file=sys.stderr)
        return 2

    report = await repair_resource_hashes(
        vault=vault,
        include_documents=include_documents,
        include_files=include_files,
        limit=limit,
    )
    print(json.dumps(report, sort_keys=True))
    return 1 if report.get("errors") else 0


async def _initialize_postgres_native(args: list[str]) -> int:
    if args:
        print("Usage: python -m app.cli initialize-postgres-native", file=sys.stderr)
        return 2
    from app.db.postgres import close_pool
    from app.services.native_revision_authority import (
        NativeAuthorityError,
        bootstrap_postgres_native,
    )

    try:
        report = await bootstrap_postgres_native()
    except NativeAuthorityError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1
    finally:
        await close_pool()
    print(json.dumps(report, sort_keys=True))
    return 0


def _native_revision_cutover_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(add_help=False)
    phases = parser.add_subparsers(dest="phase", required=True)
    plan = phases.add_parser("plan", add_help=False)
    plan.add_argument("--coverage-version", required=True)
    for phase in ("apply", "verify", "commit", "abort"):
        command = phases.add_parser(phase, add_help=False)
        command.add_argument("--cutover-id", required=True)
    retire = phases.add_parser("retire-external-git", add_help=False)
    retire.add_argument("--vault-id", required=True)
    retire.add_argument("--manifest-file", required=True)
    retire.add_argument("--idempotency-key", required=True)
    retire.add_argument("--requested-by", required=True)
    retire.add_argument("--confirm-planned-downtime", required=True)
    return parser


def _require_external_git_retirement_confirmation(vault_id: str, confirmation: str) -> uuid.UUID:
    """Require the operator's exact, vault-bound planned-downtime acknowledgement."""
    try:
        parsed_vault_id = uuid.UUID(vault_id)
    except (TypeError, ValueError):
        raise ValueError("external Git retirement vault id is invalid") from None
    expected = f"RETIRE-EXTERNAL-GIT:{parsed_vault_id}"
    if confirmation != expected:
        raise ValueError("external Git retirement requires the exact planned-downtime confirmation")
    return parsed_vault_id


async def _execute_external_git_retirement(
    *,
    vault_id: str,
    manifest_file: str,
    idempotency_key: str,
    requested_by: str,
    planned_downtime_confirmation: str,
) -> dict[str, object]:
    """Run the offline, one-vault external-Git retirement command."""
    from app.db.postgres import close_pool, get_pool, init_db
    from app.services.external_git_retirement import (
        ExternalGitRetirement,
        ExternalGitRetirementError,
        load_adoption_manifest,
    )

    try:
        parsed_vault_id = _require_external_git_retirement_confirmation(
            vault_id,
            planned_downtime_confirmation,
        )
        try:
            parsed_idempotency_key = uuid.UUID(idempotency_key)
        except (TypeError, ValueError):
            raise ValueError("external Git retirement idempotency key is invalid") from None
        manifest = load_adoption_manifest(Path(manifest_file))
        if manifest.vault_id != parsed_vault_id:
            raise ExternalGitRetirementError("external Git retirement vault id does not match the manifest")
        await init_db()
        pool = await get_pool()
        result = await ExternalGitRetirement(pool).retire(
            manifest=manifest,
            idempotency_key=parsed_idempotency_key,
            requested_by=requested_by,
        )
        if not is_dataclass(result):
            raise RuntimeError("external Git retirement returned an invalid operator receipt")
        return json.loads(json.dumps(asdict(result), default=str))
    finally:
        await close_pool()


async def _execute_native_revision_cutover(
    phase: str,
    *,
    coverage_version: str | None,
    cutover_id: str | None,
) -> dict[str, object]:
    """Run one thin operator phase over the product cutover services."""
    from app.db.postgres import close_pool, get_pool, init_db
    from app.services.git_service import GitService
    from app.services.native_revision_authority import NativeAuthorityIdentity
    from app.services.native_revision_backfill import NativeRevisionBackfill
    from app.services.native_revision_cutover import (
        CutoverVaultInput,
        NativeRevisionCutover,
        NativeRevisionCutoverVerifier,
    )

    try:
        await init_db()
        pool = await get_pool()
        git = GitService()
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=NativeRevisionCutoverVerifier(pool, git=git),
        )
        result: object
        if phase == "plan":
            assert coverage_version is not None
            async with pool.acquire() as conn:
                rows = await conn.fetch("SELECT id, name FROM vaults WHERE status <> 'deleted' ORDER BY id")
            vaults = []
            for row in rows:
                fixed_ref = await asyncio.to_thread(git.current_commit, row["name"])
                if fixed_ref is None:
                    raise ValueError(f"retained vault has no current Legacy Git ref: {row['id']}")
                vaults.append(
                    CutoverVaultInput(
                        namespace_id=row["id"],
                        fixed_ref=fixed_ref,
                    )
                )
            result = await cutover.plan(
                vaults=vaults,
                coverage_version=coverage_version,
            )
        else:
            assert cutover_id is not None
            parsed_id = uuid.UUID(cutover_id)
            if phase == "apply":
                result = await cutover.apply(parsed_id)
            elif phase == "verify":
                result = await cutover.verify(parsed_id)
            elif phase == "commit":
                result = await cutover.commit(
                    parsed_id,
                    identity=NativeAuthorityIdentity.from_settings(),
                )
            else:
                result = await cutover.abort(parsed_id)
        if not is_dataclass(result):
            raise RuntimeError("cutover phase returned an invalid operator result")
        return json.loads(json.dumps(asdict(result), default=str))
    finally:
        await close_pool()


async def _migrate_revision_backend(args: list[str]) -> int:
    try:
        parsed = _native_revision_cutover_parser().parse_args(args)
    except _CLIUsageError:
        print(MIGRATE_REVISION_BACKEND_USAGE, file=sys.stderr)
        return 2

    try:
        if parsed.phase == "retire-external-git":
            _require_external_git_retirement_confirmation(
                parsed.vault_id,
                parsed.confirm_planned_downtime,
            )
            report = await _execute_external_git_retirement(
                vault_id=parsed.vault_id,
                manifest_file=parsed.manifest_file,
                idempotency_key=parsed.idempotency_key,
                requested_by=parsed.requested_by,
                planned_downtime_confirmation=parsed.confirm_planned_downtime,
            )
        else:
            report = await _execute_native_revision_cutover(
                parsed.phase,
                coverage_version=getattr(parsed, "coverage_version", None),
                cutover_id=getattr(parsed, "cutover_id", None),
            )
    except (ValueError, RuntimeError) as exc:
        prefix = (
            "revision_backend_retirement_failed"
            if parsed.phase == "retire-external-git"
            else "revision_backend_cutover_failed"
        )
        print(f"{prefix}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


def _okf_validate(args: list[str]) -> int:
    """`okf-validate <bundle-dir>` — check a directory against OKF v0.1."""
    from pathlib import Path

    from app.services.okf import check_dir

    if len(args) != 1:
        print("Usage: python -m app.cli okf-validate <bundle-dir>", file=sys.stderr)
        return 2
    root = Path(args[0])
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2
    report = check_dir(root)
    for finding in report.findings:
        print(finding, file=sys.stderr)
    print(report.summary())
    return 0 if report.ok else 1


def _okf_export(args: list[str]) -> int:
    """`okf-export --from-git <worktree> --vault <name> --out <dir>`.

    Exports an AKB vault git worktree as an OKF bundle and validates it.
    """
    from pathlib import Path

    from app.services.okf import build_bundle, check_bundle, records_from_git_tree, write_bundle

    usage = "Usage: python -m app.cli okf-export --from-git <worktree> --vault <name> --out <dir>"
    from_git = vault = out = None
    index = 0
    while index < len(args):
        flag = args[index]
        if flag in ("--from-git", "--vault", "--out"):
            index += 1
            if index >= len(args):
                print(usage, file=sys.stderr)
                return 2
            if flag == "--from-git":
                from_git = args[index]
            elif flag == "--vault":
                vault = args[index]
            else:
                out = args[index]
        else:
            print(f"Unknown okf-export option: {flag}", file=sys.stderr)
            return 2
        index += 1
    if not (from_git and vault and out):
        print(usage, file=sys.stderr)
        return 2
    worktree = Path(from_git)
    if not worktree.is_dir():
        print(f"Not a directory: {worktree}", file=sys.stderr)
        return 2
    records = records_from_git_tree(worktree, vault)
    bundle = build_bundle(documents=records)
    write_bundle(Path(out), bundle)
    report = check_bundle(bundle)
    for finding in report.findings:
        print(finding, file=sys.stderr)
    print(f"Wrote {len(bundle)} file(s) to {out}")
    print(report.summary())
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("Usage: python -m app.cli <subcommand> [args]", file=sys.stderr)
        print(
            "Subcommands: generate-local-session-keyset, "
            "provision-recovery-admin {local|sso}, "
            "issue-recovery-admin-credential, "
            "bootstrap-standalone-sso, "
            "reset-password <username>, repair-resource-hashes, "
            "initialize-postgres-native, migrate-revision-backend, "
            "okf-validate <dir>, "
            "okf-export --from-git <worktree> --vault <name> --out <dir>",
            file=sys.stderr,
        )
        return 2
    cmd = argv[0]
    if cmd == "generate-local-session-keyset":
        return _generate_local_session_keyset(argv[1:])
    if cmd == "provision-recovery-admin":
        return asyncio.run(_provision_recovery_admin(argv[1:]))
    if cmd == "issue-recovery-admin-credential":
        return asyncio.run(_issue_recovery_admin_credential(argv[1:]))
    if cmd == "bootstrap-standalone-sso":
        return asyncio.run(_bootstrap_standalone_sso(argv[1:]))
    if cmd == "reset-password":
        if len(argv) != 2:
            print("Usage: python -m app.cli reset-password <username>", file=sys.stderr)
            return 2
        return asyncio.run(_reset_password(argv[1]))
    if cmd == "repair-resource-hashes":
        return asyncio.run(_repair_resource_hashes(argv[1:]))
    if cmd == "initialize-postgres-native":
        return asyncio.run(_initialize_postgres_native(argv[1:]))
    if cmd == "migrate-revision-backend":
        return asyncio.run(_migrate_revision_backend(argv[1:]))
    if cmd == "okf-validate":
        return _okf_validate(argv[1:])
    if cmd == "okf-export":
        return _okf_export(argv[1:])
    print(f"Unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
