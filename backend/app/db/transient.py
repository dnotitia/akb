"""Small, explicit request-error classifier; never retries an operation.

Do not classify arbitrary OSError/InterfaceError, SQL syntax, ACL or constraint
errors as availability faults. The same timeout code includes PostgreSQL's
57014 cancellation (which can also be issued by an administrator).
"""

import asyncpg

from app.exceptions import TransientDependencyError
from app.util.errors import OPERATION_TIMEOUT, SERVICE_UNAVAILABLE


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
