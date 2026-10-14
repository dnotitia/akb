"""User-facing identity normalization is shared by local and SSO paths."""

import pytest

from app.exceptions import ValidationError
from app.services.user_identity import (
    display_name_from_external,
    normalize_display_name,
    normalize_email,
    prepare_username,
    stable_username_candidate,
    username_base_from_external,
)


def test_username_prepares_case_for_database_and_preserves_unicode_letters():
    assert prepare_username("  GNU.Team  ") == "GNU.Team"
    assert prepare_username("  한글_사용자  ") == "한글_사용자"


def test_username_preserves_contextual_case_until_database_transaction():
    assert prepare_username("ΑΟΣ") == "ΑΟΣ"
    assert username_base_from_external("ΑΟΣ") == "ΑΟΣ"


@pytest.mark.parametrize(
    "value",
    ["ab", "-alice", "alice-", "alice smith", "alice@example.com"],
)
def test_username_rejects_ambiguous_or_unsafe_handles(value):
    with pytest.raises(ValidationError):
        prepare_username(value)


def test_external_handle_is_safe_and_keeps_unicode_contract():
    assert username_base_from_external("Alice Smith@example.com") == "Alice-Smith"
    assert username_base_from_external("홍 길동") == "홍-길동"
    assert username_base_from_external("@@@") == "user"


def test_stable_suffix_is_repeatable_but_does_not_expose_subject():
    first = stable_username_candidate("alice", "issuer\0private-subject")
    assert first == stable_username_candidate("alice", "issuer\0private-subject")
    assert first.startswith("alice-")
    assert "private-subject" not in first


def test_display_name_trims_and_blank_becomes_none():
    assert normalize_display_name("  Alice Kim  ") == "Alice Kim"
    assert normalize_display_name("   ") is None
    with pytest.raises(ValidationError, match="control"):
        normalize_display_name("Alice\nKim")


def test_external_display_name_is_sanitized_and_bounded_without_rejection():
    assert display_name_from_external("  Alice\n\tKim  ") == "Alice Kim"
    assert display_name_from_external("A" * 101) == "A" * 100
    assert display_name_from_external("\u202e") is None


def test_email_matches_case_insensitive_identity_contract():
    assert normalize_email("  Alice@Example.COM ") == "alice@example.com"
    with pytest.raises(ValidationError):
        normalize_email("not an email")
