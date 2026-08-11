"""Shared guards and recovery envelopes for multi-document grep replacement."""

from __future__ import annotations

from app.exceptions import ValidationError
from app.util.errors import BULK_TOO_LARGE, err, exception_envelope


# ``limit`` controls only the response preview.  Replacement has its own
# caller-declared budget so a broad pattern cannot silently turn into a larger
# write than intended.  The hard ceiling also keeps one request bounded; callers
# can narrow the vault/collection scope when a larger migration is required.
DEFAULT_MAX_REPLACEMENTS = 50
MAX_REPLACEMENTS = 1_000


def validate_max_replacements(value: int) -> int:
    """Validate the independent write budget used by grep replacement."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("max_replacements must be an integer")
    if value < 1 or value > MAX_REPLACEMENTS:
        raise ValidationError(f"max_replacements must be between 1 and {MAX_REPLACEMENTS}")
    return value


def replacement_budget_error(*, total_docs: int, max_replacements: int) -> dict:
    """Return a fail-closed envelope before any replacement write starts."""
    return err(
        f"grep replace matched {total_docs} documents, exceeding the "
        f"max_replacements budget of {max_replacements}; no writes were applied",
        code=BULK_TOO_LARGE,
        hint=(
            "Preview the scope with count_only=true or files_with_matches=true, "
            "then narrow the scope or retry with a sufficient max_replacements budget."
        ),
        total_docs=total_docs,
        max_replacements=max_replacements,
        writes_applied=0,
    )


def replacement_failure_error(
    exc: Exception,
    *,
    failed_uri: str,
    committed_replacements: int,
) -> dict:
    """Preserve a canonical error plus recovery metadata after a partial write."""
    envelope = exception_envelope(exc)
    details = dict(envelope.get("details") or {})
    details.update(
        {
            "failed_uri": failed_uri,
            "committed_replacements": committed_replacements,
        }
    )
    envelope["details"] = details
    return envelope
