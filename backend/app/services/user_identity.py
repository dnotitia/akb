"""Canonical user-facing identity values.

``users.id`` is the durable account identity.  The values in this module are
presentation and lookup attributes: a username is a stable, unique handle and
a display name is a mutable, non-unique label.  Keeping their normalization at
one boundary prevents local signup, SSO JIT and profile editing from assigning
different meanings to the same columns.
"""

from __future__ import annotations

import hashlib
import unicodedata

from app.exceptions import ValidationError


USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 32
DISPLAY_NAME_MAX_LENGTH = 100
_HANDLE_PUNCTUATION = frozenset("._-")


def prepare_username(value: str) -> str:
    """Validate a local handle while preserving case for database lowering.

    PostgreSQL is the uniqueness backstop for handles. Python casing cannot
    reproduce every database/locale rule (notably contextual Greek sigma), so
    callers must pass this prepared value through PostgreSQL ``lower()`` in
    the same transaction that checks and stores it.
    """
    username = unicodedata.normalize("NFC", value).strip()
    if not (USERNAME_MIN_LENGTH <= len(username) <= USERNAME_MAX_LENGTH):
        raise ValidationError(
            f"Username must be {USERNAME_MIN_LENGTH}-{USERNAME_MAX_LENGTH} characters"
        )
    if (
        not username[0].isalnum()
        or not username[-1].isalnum()
        or any(not (char.isalnum() or char in _HANDLE_PUNCTUATION) for char in username)
    ):
        raise ValidationError(
            "Username may contain letters, numbers, '.', '_' and '-', "
            "and must start and end with a letter or number"
        )
    return username


def username_base_from_external(value: str | None) -> str:
    """Build a safe handle base from an external claim without trusting it.

    OIDC providers are free to send spaces or e-mail addresses in
    ``preferred_username``. AKB preserves Unicode letters and numbers (the
    existing product contract includes Hangul handles) while replacing unsafe
    separators with one portable dash.
    """
    # Preserve case until the storage transaction applies PostgreSQL lower().
    raw = unicodedata.normalize("NFKC", value or "").strip()
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    pieces: list[str] = []
    unsafe = False
    for char in raw:
        if char.isalnum() or char in _HANDLE_PUNCTUATION:
            pieces.append(char)
            unsafe = False
        elif not unsafe:
            pieces.append("-")
            unsafe = True
    candidate = "".join(pieces).strip("._-")
    candidate = candidate[:USERNAME_MAX_LENGTH].rstrip("._-")
    if len(candidate) < USERNAME_MIN_LENGTH:
        return "user"
    return candidate


def stable_username_candidate(base: str, stable_key: str, attempt: int = 0) -> str:
    """Return a stable collision suffix without exposing the external subject."""
    digest = hashlib.sha256(f"{stable_key}:{attempt}".encode()).hexdigest()[:6]
    stem_limit = USERNAME_MAX_LENGTH - len(digest) - 1
    stem = base[:stem_limit].rstrip("._-") or "user"
    return f"{stem}-{digest}"


def normalize_display_name(value: str | None) -> str | None:
    """Trim a display label, map blank to NULL and reject control characters."""
    if value is None:
        return None
    display_name = unicodedata.normalize("NFC", value).strip()
    if not display_name:
        return None
    if len(display_name) > DISPLAY_NAME_MAX_LENGTH:
        raise ValidationError(
            f"Display name must be at most {DISPLAY_NAME_MAX_LENGTH} characters"
        )
    if any(unicodedata.category(char) == "Cc" for char in display_name):
        raise ValidationError("Display name cannot contain control characters")
    return display_name


def display_name_from_external(value: str | None) -> str | None:
    """Safely bound an IdP presentation claim without blocking login.

    Local profile input is rejected when malformed so the user can correct it.
    An already-bound external identity cannot correct its upstream claims at
    AKB's login boundary, so control/format characters are removed, whitespace
    is collapsed and the result is bounded instead of raising an auth error.
    """
    if value is None:
        return None
    normalized = unicodedata.normalize("NFC", value)
    cleaned = "".join(
        " " if char.isspace() else "" if unicodedata.category(char).startswith("C") else char
        for char in normalized
    )
    display_name = " ".join(cleaned.split())[:DISPLAY_NAME_MAX_LENGTH].rstrip()
    return display_name or None


def normalize_email(value: str) -> str:
    """Match AKB's existing case-insensitive e-mail identity contract."""
    email = unicodedata.normalize("NFC", value).strip().lower()
    if not email or "@" not in email or any(char.isspace() for char in email):
        raise ValidationError("Enter a valid email address")
    return email
