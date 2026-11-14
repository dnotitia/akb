"""Small, explicit request-error classifier; never retries an operation.

Do not classify arbitrary OSError/InterfaceError, SQL syntax, ACL or constraint
errors as availability faults. The same timeout code includes PostgreSQL's
57014 cancellation (which can also be issued by an administrator).
"""

import asyncpg

from app.exceptions import TransientDependencyError
from app.util.errors import OPERATION_TIMEOUT, SERVICE_UNAVAILABLE, WRITE_BUSY

# The constraint name the deadline of a Vault's event lock carries, with
# SQLSTATE 55P03 (migration 132, `akb_lock_vault_events`). Only that error is a
# retryable `write_busy`. A write that does work its transaction cannot undo,
# such as a Bare Git commit, takes the Vault's event lock before that work, so
# the failed request changed nothing. Any other lock wait past its limit, for
# example a `lock_timeout` set on a role, can end a transaction after such work,
# and a deadlock (40P01) can too; both stay internal errors.
EVENT_LOCK_TIMEOUT = "akb_event_lock_timeout"


def is_event_lock_timeout(exc: BaseException) -> bool:
    """True for the timeout of the wait for a Vault's event lock."""
    return (
        isinstance(exc, asyncpg.PostgresError)
        and getattr(exc, "sqlstate", None) == "55P03"
        and getattr(exc, "constraint_name", None) == EVENT_LOCK_TIMEOUT
    )


def transient_error(exc: Exception) -> TransientDependencyError | None:
    if isinstance(exc, TransientDependencyError):
        return exc
    # asyncpg0.31 can replace the query's connection-loss exception with an
    # InterfaceError while exiting its now-closed transaction. Preserve only
    # a typed transient cause; an ordinary InterfaceError remains a bug.
    cause = exc.__cause__ or exc.__context__
    if isinstance(exc, asyncpg.InterfaceError) and isinstance(
        cause, (asyncpg.PostgresError, TimeoutError, ConnectionError),
    ):
        exc = cause
    if isinstance(exc, (TimeoutError, asyncpg.QueryCanceledError)):
        return TransientDependencyError(
            code=OPERATION_TIMEOUT,
            message="The operation timed out or was cancelled by the database.",
        )
    # Use SQLSTATE rather than messages: messages can contain SQL, hostnames,
    # credentials, or localized text. Protocol/auth/configuration faults are
    # deliberately not folded into this list.
    sqlstate = getattr(exc, "sqlstate", None)
    if is_event_lock_timeout(exc):
        return TransientDependencyError(
            code=WRITE_BUSY,
            message=(
                "Another write of this Vault held its event lock past the limit; "
                "this request's transaction was rolled back."
            ),
            status_code=429,
            hint=(
                "Retry after a short delay. The failed transaction wrote nothing; if the "
                "request made several writes, check which earlier ones completed."
            ),
            retry_after_secs=1,
        )
    unavailable = isinstance(exc, ConnectionError) or (
        isinstance(exc, asyncpg.PostgresError)
        and sqlstate in {
            "08000", "08001", "08003", "08006", "08007",
            "57P01", "57P02", "57P03", "53300", "53200",
        }
    )
    if unavailable:
        return TransientDependencyError(
            code=SERVICE_UNAVAILABLE,
            message="A required service is temporarily unavailable.",
        )
    return None
