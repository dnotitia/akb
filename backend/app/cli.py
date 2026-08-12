"""AKB management CLI.

Invoke via:
    docker compose exec backend python -m app.cli <subcommand> [args]
or, on a server with the backend installed:
    python -m app.cli <subcommand> [args]

The backend container is pip-installed (no uv inside). Use plain `python`
in all in-container invocations.

Subcommands:
    provision-recovery-admin   Explicitly provision the designated local or
                               SSO recovery administrator. Password material
                               is accepted only by file/stdin and is never
                               printed.
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
from pathlib import Path
from typing import NoReturn


REPAIR_RESOURCE_HASHES_USAGE = (
    "Usage: python -m app.cli repair-resource-hashes "
    "[--vault NAME] [--documents-only|--files-only] [--limit N]"
)

PROVISION_RECOVERY_ADMIN_USAGE = (
    "Usage: python -m app.cli provision-recovery-admin "
    "{local|sso} --username USER --email EMAIL [profile options]\n"
    "  local: (--password-file PATH|- | --generate-password-file PATH)\n"
    "  sso:   --issuer ISSUER --subject SUBJECT"
)


class _CLIUsageError(Exception):
    pass


class _ProvisioningInputError(Exception):
    pass


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
        text = (
            sys.stdin.read()
            if source == "-"
            else Path(source).read_text(encoding="utf-8")
        )
    except OSError:
        raise _ProvisioningInputError("Unable to read the password source") from None
    password = text.rstrip("\r\n")
    if not password:
        raise _ProvisioningInputError("The password source is empty")
    if "\r" in password or "\n" in password:
        raise _ProvisioningInputError("The password source must contain one line")
    return password


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
    except (OSError, ValueError):
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
    from app.db.postgres import close_pool, init_db
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
        await init_db()
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


async def _reset_password(username: str) -> int:
    from app.exceptions import AKBError, NotFoundError
    from app.services.password_service import reset_password

    try:
        temp, uname = await reset_password(
            username=username, actor_id=None, method="cli",
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

    usage = (
        "Usage: python -m app.cli okf-export --from-git <worktree> "
        "--vault <name> --out <dir>"
    )
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
            "Subcommands: provision-recovery-admin {local|sso}, "
            "reset-password <username>, repair-resource-hashes, "
            "initialize-postgres-native, okf-validate <dir>, "
            "okf-export --from-git <worktree> --vault <name> --out <dir>",
            file=sys.stderr,
        )
        return 2
    cmd = argv[0]
    if cmd == "provision-recovery-admin":
        return asyncio.run(_provision_recovery_admin(argv[1:]))
    if cmd == "reset-password":
        if len(argv) != 2:
            print("Usage: python -m app.cli reset-password <username>", file=sys.stderr)
            return 2
        return asyncio.run(_reset_password(argv[1]))
    if cmd == "repair-resource-hashes":
        return asyncio.run(_repair_resource_hashes(argv[1:]))
    if cmd == "initialize-postgres-native":
        return asyncio.run(_initialize_postgres_native(argv[1:]))
    if cmd == "okf-validate":
        return _okf_validate(argv[1:])
    if cmd == "okf-export":
        return _okf_export(argv[1:])
    print(f"Unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
