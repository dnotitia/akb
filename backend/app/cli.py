"""AKB management CLI.

Invoke via:
    docker compose exec backend python -m app.cli <subcommand> [args]
or, on a server with the backend installed:
    python -m app.cli <subcommand> [args]

The backend container is pip-installed (no uv inside). Use plain `python`
in all in-container invocations.

Subcommands:
    reset-password <username>   Generate a temp password for the given user.
                                 Prints the temp password to stdout. Caller
                                 must share it with the user out-of-band.
    repair-resource-hashes      Backfill document/file content-hash projections.
"""
from __future__ import annotations

import asyncio
import json
import sys


REPAIR_RESOURCE_HASHES_USAGE = (
    "Usage: python -m app.cli repair-resource-hashes "
    "[--vault NAME] [--documents-only|--files-only] [--limit N]"
)


async def _reset_password(username: str) -> int:
    from app.exceptions import NotFoundError
    from app.services.password_service import reset_password

    try:
        temp, uname = await reset_password(
            username=username, actor_id=None, method="cli",
        )
    except NotFoundError:
        print(f"User not found: {username}", file=sys.stderr)
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
            "Subcommands: reset-password <username>, repair-resource-hashes, "
            "okf-validate <dir>, okf-export --from-git <worktree> --vault <name> --out <dir>",
            file=sys.stderr,
        )
        return 2
    cmd = argv[0]
    if cmd == "reset-password":
        if len(argv) != 2:
            print("Usage: python -m app.cli reset-password <username>", file=sys.stderr)
            return 2
        return asyncio.run(_reset_password(argv[1]))
    if cmd == "repair-resource-hashes":
        return asyncio.run(_repair_resource_hashes(argv[1:]))
    if cmd == "okf-validate":
        return _okf_validate(argv[1:])
    if cmd == "okf-export":
        return _okf_export(argv[1:])
    print(f"Unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
