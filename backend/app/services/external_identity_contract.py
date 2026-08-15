"""Shared bounds for identity-provider claims used as durable account keys."""

from __future__ import annotations


OIDC_SUBJECT_MAX_LENGTH = 1024


def bounded_nonempty_claim(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        return None
    return value
