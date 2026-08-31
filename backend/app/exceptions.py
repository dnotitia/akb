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
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        hint: str | None = None,
        details: dict | None = None,
    ):
        super().__init__(
            message,
            status_code=409,
            code=code,
            hint=hint,
            details=details,
        )


DOCUMENT_TITLE_CONFLICT = "document_title_conflict"


class DocumentTitleConflictError(ConflictError):
    """An interactive write found the same title in one Collection.

    Title uniqueness remains a UI policy, not a storage constraint. The
    structured payload lets clients offer "open existing" and an explicit
    keep-both retry without parsing prose or exposing the collision-safe slug.
    """

    def __init__(
        self,
        *,
        title: str,
        collection: str,
        existing_path: str,
        existing_title: str,
    ):
        location = collection or "Vault root"
        super().__init__(
            f'A document titled "{title.strip()}" already exists in {location}',
            code=DOCUMENT_TITLE_CONFLICT,
            hint="Open the existing document, choose another title or Collection, or explicitly keep both.",
            details={
                "title": title.strip(),
                "collection": collection,
                "existing_path": existing_path,
                "existing_title": existing_title,
            },
        )


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


class CredentialChangeRequiredError(AKBError):
    """A delivered local credential has not been replaced by its holder yet.

    Local mode's counterpart to the identity provider's ``UPDATE_PASSWORD``
    required action. The account authenticates, but the only thing the
    resulting session may do is replace the credential it was issued.
    """

    def __init__(self):
        super().__init__(
            "This account must change its password before doing anything else",
            status_code=403,
            code="credential_change_required",
        )


class ExternalIdentityConflictError(AKBError):
    """Verified external claims conflict with an existing stable binding."""

    def __init__(self):
        super().__init__(
            "External identity conflicts with an existing AKB account",
            status_code=409,
            code="identity_conflict",
        )


class ExternalIdentityAdoptionNotRequestedError(AKBError):
    """A binding would have attached to an account chosen by email address.

    ``ensure_human_external_identity`` may attach a new identity to an existing
    unbound account holding the same address. For a control plane that already
    knows which person it is provisioning, that is a convenience. For approving
    a recorded arrival it is not: the address there is a claim out of the
    token, so letting it select the account makes the approval an adoption by
    address at exactly the step that exists to stop it.

    The candidate account is named because an administrator who *does* mean to
    attach the arrival to it can then say so deliberately, by passing it back as
    ``existing_user_id`` — which is a different act from having it chosen for
    them.
    """

    def __init__(self, candidate_user_id: str):
        super().__init__(
            "An account already holds this email address; name it explicitly to attach this identity to it",
            status_code=409,
            code="external_identity_adoption_not_requested",
            details={"candidate_user_id": candidate_user_id},
        )


class ExternalIdentityIssuerMismatchError(AKBError):
    """A prelink named an issuer this runtime does not present.

    The binding a control plane writes is only usable if its issuer is the one
    this AKB will actually see on a token. Nothing downstream re-checks that:
    ``invite_only`` matches an exact ``(issuer, subject)`` pair, so a binding
    written under any other issuer is created successfully, reported as
    success, and refuses its owner at sign-in with nothing recording why.

    The expected issuer is included because it is public — it is the ``iss``
    claim this deployment stamps and is published in its discovery document —
    and because a caller that cannot see it cannot correct the call.
    """

    def __init__(self, expected: str):
        super().__init__(
            "External identity issuer does not match the one this AKB presents",
            status_code=422,
            code="external_identity_issuer_mismatch",
            details={"expected_issuer": expected},
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


class RecoveryAdminCredentialAuthorizationError(AKBError):
    """The credential-issue caller is not an independent service administrator."""

    def __init__(self):
        super().__init__(
            "Recovery administrator credential issue requires an independent service administrator token",
            status_code=403,
            code="recovery_admin_credential_requires_service_admin",
        )


class RecoveryAdminCredentialConflictError(AKBError):
    """The expected recovery identity is not one that can be issued a credential."""

    def __init__(self):
        super().__init__(
            "Recovery administrator does not match the credential-issue contract",
            status_code=409,
            code="recovery_admin_credential_conflict",
        )


class RecoveryAdminCredentialUnavailableError(AKBError):
    """No authority in this installation can replace the stored credential."""

    def __init__(self):
        super().__init__(
            "Cannot rotate the recovery administrator credential in the configured auth mode: "
            "the identity provider holds it and nothing in a running AKB can replace it",
            status_code=503,
            code="recovery_admin_credential_rotation_unavailable",
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
