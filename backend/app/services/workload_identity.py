"""Read rotating projected tokens without retaining or logging their values."""

from pathlib import Path

from app.exceptions import AKBError

_MAX_TOKEN_BYTES = 64 * 1024


class WorkloadIdentityError(AKBError):
    def __init__(self, message: str = "Workload identity is unavailable"):
        super().__init__(message, status_code=503)


def read_projected_token(path: str) -> str:
    # Kubernetes atomically replaces symlinks when it rotates a projection.
    # Open the configured path on each call, never a cached/resolved handle.
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_TOKEN_BYTES + 1)
        token = raw.decode("ascii").strip()
    except (OSError, UnicodeError, ValueError):
        raise WorkloadIdentityError() from None
    if len(raw) > _MAX_TOKEN_BYTES or not token or any(c.isspace() or not c.isprintable() for c in token):
        raise WorkloadIdentityError()
    return token
