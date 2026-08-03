"""Tests for the process-wide secret-aware log filter.

Covers the redaction patterns (secrets removed, normal messages untouched), the
``SecretRedactingFilter`` record rewriting (args-based and message-based, plus
cached-traceback scrub), and ``install_secret_redaction`` coverage of a child
logger via a root handler.
"""

from __future__ import annotations

import io
import logging

import pytest

from app.logging_redaction import (
    SecretRedactingFilter,
    _FILTER,
    install_secret_redaction,
    redact,
)

# A real base64 of "x-access-token:s3cr3t-token-value" — the shape the runner
# builds for the Authorization header.
_B64_CRED = "eC1hY2Nlc3MtdG9rZW46czNjcjN0LXRva2VuLXZhbHVl"  # pragma: allowlist secret


# ── redact() — secrets removed ───────────────────────────────────────
@pytest.mark.parametrize(
    "text, secret",
    [
        (
            "cloning https://x-access-token:ghp_abcdEFGH1234wxyz@github.com/o/r.git",  # pragma: allowlist secret
            "ghp_abcdEFGH1234wxyz",
        ),
        (f"header Authorization: Basic {_B64_CRED} sent", _B64_CRED),
        (f"bare Basic {_B64_CRED} reflected", _B64_CRED),
        ("token=ghp_ABCdef0123456789ghij for repo", "ghp_ABCdef0123456789ghij"),
        ("github_pat_11ABCDE0123456789_abcDEFghiJKL rotated", "github_pat_11ABCDE"),
        ("gitlab glpat-ABCdef0123456789 leaked", "glpat-ABCdef0123456789"),
        ("x-access-token: s3cr3t-token-value-here in header", "s3cr3t-token-value-here"),
        (
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        ),
        # An all-alphabetic bare Bearer value (no digit/`+`/`=`) must be
        # redacted too — previously the pattern required a non-letter and leaked.
        ("reflected Bearer AbcdEFGHijklMNOPqrst in error body", "AbcdEFGHijklMNOPqrst"),
        # An x-access-token value that itself contains whitespace (here a
        # reflected `Basic <b64>`) must be redacted to the field delimiter, not
        # truncated at the first space — otherwise the b64 tail leaked.
        ("x-access-token: Basic dXNlcjpwYXNzd29yZA== trailing", "dXNlcjpwYXNzd29yZA=="),
    ],
)
def test_redact_removes_secret(text: str, secret: str) -> None:
    out = redact(text)
    assert secret not in out, f"secret leaked: {out!r}"
    assert "<redacted>" in out


def test_redact_url_userinfo_keeps_host() -> None:
    out = redact("fetch https://user:p@ssw0rd@git.example.com/o/r.git failed")  # pragma: allowlist secret
    assert "p@ssw0rd" not in out
    assert "user" not in out.split("@")[0].split("://")[1]  # userinfo gone
    assert "git.example.com" in out  # host preserved for diagnostics
    assert "<redacted>@" in out


def test_redact_is_idempotent() -> None:
    once = redact(f"Authorization: Basic {_B64_CRED}")
    assert redact(once) == once


# ── redact() — normal messages unaffected (no over-redaction) ────────
@pytest.mark.parametrize(
    "text",
    [
        "Backfilled external-git mirror marker on 3 vault(s)",
        "Basic authentication is enabled for this realm",  # 'Basic' + English word
        "Bearer token required but not supplied",  # 'Bearer' + short words
        "Bearer token authentication required for this route",  # multi-word prose
        "x-access-token header is missing from the request",  # no ':'/'=' → not a value
        "commit 9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e reconciled",  # 40-hex SHA
        "disk-usage on /data/vaults is high",  # must not trip the sk- token rule
        "processed 12345 tokens across 42 documents",
        "user alice@example.com updated vault settings",  # bare email, no scheme
        "vault_id=0192f3a4-5b6c-7d8e-9f01-234567890abc created",  # a UUID
    ],
)
def test_redact_leaves_normal_message_unchanged(text: str) -> None:
    assert redact(text) == text


# ── SecretRedactingFilter — record rewriting ─────────────────────────
def _record(msg: str, args=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="akb.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
    )


def test_filter_redacts_arg_rendered_secret() -> None:
    filt = SecretRedactingFilter()
    rec = _record("cloning %s", ("https://x-access-token:ghp_SECRETtoken1234@h/r.git",))  # pragma: allowlist secret
    assert filt.filter(rec) is True
    rendered = rec.getMessage()
    assert "ghp_SECRETtoken1234" not in rendered
    assert rec.args is None  # folded once a secret was found


def test_filter_redacts_message_secret() -> None:
    filt = SecretRedactingFilter()
    rec = _record(f"Authorization: Basic {_B64_CRED}")
    assert filt.filter(rec) is True
    assert _B64_CRED not in rec.getMessage()


def test_filter_leaves_benign_record_lazy() -> None:
    filt = SecretRedactingFilter()
    rec = _record("count=%d done", (5,))
    assert filt.filter(rec) is True
    # No secret → args preserved (lazy formatting intact) and message correct.
    assert rec.args == (5,)
    assert rec.getMessage() == "count=5 done"


def test_filter_scrubs_cached_exc_text() -> None:
    filt = SecretRedactingFilter()
    rec = _record("operation failed")
    rec.exc_text = "Traceback: connection to https://tok:ghp_XYZ12345678@h refused"  # pragma: allowlist secret
    assert filt.filter(rec) is True
    assert "ghp_XYZ12345678" not in rec.exc_text


def test_filter_redacts_live_exception_traceback() -> None:
    """Regression: ``logger.exception()`` leaves ``exc_info`` set but
    ``exc_text`` empty at filter time, so the handler's ``Formatter`` renders the
    traceback AFTER the filter runs. The filter must pre-render + redact the
    traceback (which carries the raw exception message) so no secret reaches the
    emitted stream. This is the gap the ``exc_text``-only tests above did not
    cover — here we drive a real ``logger.exception()`` end-to-end."""
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    try:
        install_secret_redaction()
        logger = logging.getLogger("akb.exc.redaction_test")
        logger.propagate = True
        logger.setLevel(logging.ERROR)
        secret_url = "https://x-access-token:ghp_LIVEtoken12345678@github.com/o/r.git"  # pragma: allowlist secret
        try:
            raise RuntimeError(f"clone failed for {secret_url} using Basic {_B64_CRED}")
        except RuntimeError:
            logger.exception("mirror sync failed")  # message itself is clean
        handler.flush()
        out = stream.getvalue()
        assert "ghp_LIVEtoken12345678" not in out  # token in URL userinfo gone
        assert _B64_CRED not in out  # reflected `Basic <b64>` gone
        assert "x-access-token:ghp" not in out  # the userinfo pair gone
        assert "<redacted>" in out  # redaction actually fired in the traceback
        assert "RuntimeError" in out  # traceback still emitted (diagnostics intact)
    finally:
        root.removeHandler(handler)


def test_filter_never_raises_on_bad_str() -> None:
    class Boom:
        def __str__(self) -> str:  # noqa: D401
            raise ValueError("boom")

    filt = SecretRedactingFilter()
    rec = _record("value %s", (Boom(),))
    # Must not propagate — logging is never allowed to break.
    assert filt.filter(rec) is True


# ── install_secret_redaction — process-wide coverage ─────────────────
def test_install_covers_child_logger_via_root_handler() -> None:
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    try:
        install_secret_redaction()
        child = logging.getLogger("akb.child.redaction_test")
        child.propagate = True
        child.setLevel(logging.INFO)  # else INFO is gated by root's WARNING default
        child.info("mirror https://x-access-token:ghp_leakME12345678@h/r.git")  # pragma: allowlist secret
        handler.flush()
        out = stream.getvalue()
        assert "ghp_leakME12345678" not in out
        assert "<redacted>" in out
    finally:
        root.removeHandler(handler)


def test_install_is_idempotent() -> None:
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root.addHandler(handler)
    try:
        install_secret_redaction()
        install_secret_redaction()
        assert sum(1 for f in handler.filters if f is _FILTER) == 1
    finally:
        root.removeHandler(handler)
