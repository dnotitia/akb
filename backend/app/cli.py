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
"""
from __future__ import annotations

import asyncio
import sys


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


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("Usage: python -m app.cli <subcommand> [args]", file=sys.stderr)
        print("Subcommands: reset-password <username>", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "reset-password":
        if len(argv) != 2:
            print("Usage: python -m app.cli reset-password <username>", file=sys.stderr)
            return 2
        return asyncio.run(_reset_password(argv[1]))
    print(f"Unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
