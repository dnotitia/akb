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


class MirrorMarkerError(AKBError):
    """The on-disk external-git mirror marker is in an AMBIGUOUS state → 503.

    Raised when the ``_MIRROR_MARKER`` entry at a bare
    repo's root is neither cleanly absent (a manual, non-mirror vault) nor a
    genuine regular-file marker, but something ambiguous — a directory, a
    symlink (incl. broken), another file type, or an unexpected stat error.

    Collapsing such an entry to "not a mirror" would let the vault's reads fall
    through to GitPython, re-opening the promisor/rewrite lazy-fetch surface
    the hermetic-runner routing closes. So the marker check is fail-CLOSED: it
    raises this rather than returning a boolean, surfacing as a 503 on the read
    path (and, at startup backfill, as a boot-abort) instead of serving open.
    The message is value-less (the marker filename only) — no path or secret.
    """

    def __init__(self, message: str):
        super().__init__(
            message,
            status_code=503,
            code="external_git_mirror_marker_abnormal",
        )


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


class ServiceIdentityAdoptionError(AKBError):
    """A local bootstrap administrator cannot be safely adopted in place."""

    def __init__(self):
        super().__init__(
            "Bootstrap administrator does not match the service identity adoption contract",
            status_code=409,
            code="service_identity_adoption_conflict",
        )


class RecoveryAdminConflictError(AKBError):
    """Provisioning input conflicts with the designated recovery identity."""

    def __init__(self):
        super().__init__(
            "Recovery administrator conflicts with existing account state",
            status_code=409,
            code="recovery_admin_conflict",
        )


class RecoveryAdminModeError(AKBError):
    """The requested provisioning profile is not the configured auth mode."""

    def __init__(self):
        super().__init__(
            "Recovery administrator provisioning does not match the configured auth mode",
            status_code=409,
            code="recovery_admin_mode_mismatch",
        )


class RecoveryAdminProtectedError(AKBError):
    """The designated recovery identity must remain an administrator."""

    def __init__(self):
        super().__init__(
            "The designated recovery administrator cannot be demoted or deleted",
            status_code=409,
            code="recovery_admin_protected",
        )


class RecoveryAdminRetirementAuthorizationError(AKBError):
    """The retirement caller is not an independent service administrator."""

    def __init__(self):
        super().__init__(
            "Recovery administrator retirement requires an independent service administrator token",
            status_code=403,
            code="recovery_admin_retirement_requires_service_admin",
        )


class RecoveryAdminRetirementConflictError(AKBError):
    """The expected recovery identity does not match one safe retirement state."""

    def __init__(self):
        super().__init__(
            "Recovery administrator does not match the retirement contract",
            status_code=409,
            code="recovery_admin_retirement_conflict",
        )


class ExternalAuthDisabledError(AKBError):
    """External authentication is disabled by deployment policy."""

    def __init__(self):
        super().__init__(
            "External authentication is disabled",
            status_code=403,
            code="external_auth_disabled",
        )


class BrowserSessionNotReadyError(AKBError):
    """Human SSO is selected, but browser custody is not configured."""

    def __init__(self):
        super().__init__(
            "SSO browser session custody is not configured",
            status_code=503,
            code="browser_session_not_ready",
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
            f"Write queue for vault '{vault}' is saturated (gave up after {waited_secs:.0f}s); retry shortly",
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
