"""Application-level exceptions.

Services raise these; the global handler in main.py maps them to HTTP responses.
"""


class AKBError(Exception):
    """Base exception for all AKB errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
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


class ValidationError(AKBError):
    def __init__(self, message: str):
        super().__init__(message, status_code=422)
