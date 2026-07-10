"""Application-level exceptions.

Services raise these; the global handler in main.py maps them to HTTP responses.
"""


class AKBError(Exception):
    """Base exception for all AKB errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        *,
        code: str | None = None,
        hint: str | None = None,
        details: dict | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.code = code
        self.hint = hint
        self.details = details
        super().__init__(message)


class NotFoundError(AKBError):
    def __init__(self, resource: str, identifier: str):
        super().__init__(f"{resource} not found: {identifier}", status_code=404)


class ConflictError(AKBError):
    def __init__(self, message: str):
        super().__init__(message, status_code=409)


class AuthenticationError(AKBError):
    def __init__(self, message: str = "Invalid or expired credentials"):
        super().__init__(message, status_code=401)


class ForbiddenError(AKBError):
    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__(message, status_code=403)


class LocalAuthDisabledError(AKBError):
    """Stable, non-sensitive denial for disabled password authentication."""

    def __init__(self):
        super().__init__(
            "Local password authentication is disabled",
            status_code=403,
            code="local_auth_disabled",
        )


class MembershipRequiredError(AKBError):
    """A verified external user has no pre-provisioned AKB membership."""

    def __init__(self):
        super().__init__(
            "This account is not provisioned for this AKB workspace",
            status_code=403,
            code="membership_required",
        )


class AccountSuspendedError(AKBError):
    """The AKB-local account exists but is administratively suspended."""

    def __init__(self):
        super().__init__(
            "This AKB account is suspended",
            status_code=403,
            code="account_suspended",
        )


class ExternalIdentityConflictError(AKBError):
    """Verified external claims conflict with an existing stable binding."""

    def __init__(self):
        super().__init__(
            "External identity conflicts with an existing AKB account",
            status_code=409,
            code="identity_conflict",
        )


class ExternalAuthDisabledError(AKBError):
    """External authentication is disabled by deployment policy."""

    def __init__(self):
        super().__init__(
            "External authentication is disabled",
            status_code=403,
            code="external_auth_disabled",
        )


class CredentialCleanupIncompleteError(AKBError):
    """Credential denial landed, but strict derived-role cleanup is pending."""

    def __init__(self, token_ids: list[str]):
        super().__init__(
            "Credential revocation is active but derived-role cleanup is incomplete",
            status_code=503,
            code="credential_cleanup_incomplete",
            details={"token_ids": token_ids},
        )


class PasswordLifecycleUnavailableError(AKBError):
    """The account intentionally has no local password lifecycle."""

    def __init__(self):
        super().__init__(
            "Local password management is unavailable for this account",
            status_code=409,
            code="password_lifecycle_unavailable",
        )


class ExternalProfileReadOnlyError(AKBError):
    """OIDC/service profile identity fields are controlled externally."""

    def __init__(self):
        super().__init__(
            "Profile identity fields are managed by the external identity provider",
            status_code=409,
            code="external_profile_read_only",
        )


class WriteBusyError(AKBError):
    """Write admission timed out → HTTP 429.

    Raised by the write lane (services/write_lane.py) when a git-committing
    write waited out its queue deadline (per-vault gate + global lane), or
    when the global waiter backstop is full. The request performed no work —
    retrying after a short backoff is always safe. ``retry_after_secs`` is
    surfaced as the HTTP ``Retry-After`` header and in the MCP error envelope.
    """

    def __init__(self, vault: str, waited_secs: float, retry_after_secs: int = 5):
        self.retry_after_secs = retry_after_secs
        super().__init__(
            f"Write queue for vault '{vault}' is saturated "
            f"(gave up after {waited_secs:.0f}s); retry shortly",
            status_code=429,
        )


class ValidationError(AKBError, ValueError):
    """Client-input error → HTTP 422.

    Also IS-A ``ValueError`` so the many ``except ValueError`` sites (MCP tool
    handlers, the alter-table guard tests) keep catching validation rejects as
    invalid-argument rather than letting a service-layer reject leak out as an
    internal 500. AKBError comes first in the MRO, so ``status_code`` stays 422.
    """

    def __init__(self, message: str):
        super().__init__(message, status_code=422)


class InvalidColumnTypeError(AKBError, ValueError):
    """Unsupported dynamic-table column type → HTTP 400 invalid_column_type."""

    def __init__(self, message: str, *, hint: str | None = None, details: dict | None = None):
        super().__init__(
            message,
            status_code=400,
            code="invalid_column_type",
            hint=hint,
            details=details,
        )
