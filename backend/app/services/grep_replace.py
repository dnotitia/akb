"""Shared guards and recovery envelopes for multi-document grep replacement."""

from __future__ import annotations

import re

from app.exceptions import ValidationError
from app.util.errors import BULK_TOO_LARGE, err, exception_envelope


# ``limit`` controls only the response preview.  Replacement has its own
# caller-declared budget so a broad pattern cannot silently turn into a larger
# write than intended.  The hard ceiling also keeps one request bounded; callers
# can narrow the vault/collection scope when a larger migration is required.
DEFAULT_MAX_REPLACEMENTS = 50
MAX_REPLACEMENTS = 1_000


def apply_grep_replacement(
    text: str,
    pattern: str,
    replacement: str,
    *,
    regex: bool,
    case_sensitive: bool,
) -> str:
    """Apply grep replacement while keeping literal-mode replacements literal."""
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.sub(pattern, replacement, text, flags=flags)
    if case_sensitive:
        return text.replace(pattern, replacement)

    # ``re.sub`` is needed for case-insensitive literal matching, but a plain
    # replacement string would still interpret ``\\1``, ``\\t``, and similar
    # sequences as a regex replacement template.  A callable preserves the
    # caller's replacement bytes exactly; backreferences remain available only
    # in the explicitly requested regex mode.
    return re.sub(
        re.escape(pattern),
        lambda _match: replacement,
        text,
        flags=re.IGNORECASE,
    )


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


# How many committed replacements a partial failure's hint names by URI.
_HINTED_REPLACEMENTS = 10


def replacement_failure_error(
    exc: Exception,
    *,
    failed_uri: str,
    committed_replacements: int,
    completed: list[dict] | None = None,
) -> dict:
    """Preserve a canonical error plus recovery metadata after a partial write.

    Replacements commit document by document, and ``completed`` holds the
    receipts of those that committed before the failure (each with its URI
    and its new revision, a Native revision or a Git commit). When there are
    any, the envelope says the request partly completed
    (``details.partially_applied``), and its hint names them and asks to
    apply the replacement only to the documents not replaced yet: the same
    request sent again would replace the text again in a replaced document
    whose new text still matches the pattern. The error's own hint, such as a
    ``write_busy`` hint that says nothing was written, would be wrong then.
    """
    envelope = exception_envelope(exc)
    details = dict(envelope.get("details") or {})
    details.update(
        {
            "failed_uri": failed_uri,
            "committed_replacements": committed_replacements,
        }
    )
    if committed_replacements:
        details["partially_applied"] = True
        receipts = completed or []
        named = ", ".join(
            f"{item['uri']} ({item.get('revision') or item.get('commit')})"
            for item in receipts[:_HINTED_REPLACEMENTS]
        )
        more = (
            f" and {len(receipts) - _HINTED_REPLACEMENTS} more"
            if len(receipts) > _HINTED_REPLACEMENTS else ""
        )
        envelope["hint"] = (
            f"{committed_replacements} replacement(s) committed before this failure and are "
            f"listed in `replacements`: {named}{more}. Do not send the same request again: it "
            "would replace the text again in any of those documents that still matches the "
            "pattern. Apply the replacement only to the documents not replaced yet, starting "
            f"with {failed_uri}."
        )
    envelope["details"] = details
    return envelope
