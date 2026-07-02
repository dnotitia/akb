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
