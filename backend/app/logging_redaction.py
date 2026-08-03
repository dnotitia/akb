"""Process-wide, secret-aware log redaction (global belt).

The hermetic external-git runner sanitizes its OWN errors at the source
(``external_git_runner._sanitize`` strips the URL, token, credential header, and
any ``Basic <base64>`` before an error can reach a logger or the DB). This module
is the **belt** behind that suspenders: a single ``logging.Filter`` installed on
every real handler so that if a secret ever reaches the logging subsystem by some
other path — a third-party library logging a request URL, a stray f-string, an
exception message we did not wrap — it is redacted before it is written out.

Why a filter on the HANDLERS (not just a logger)
-------------------------------------------------
In Python's logging model a ``Filter`` attached to a *logger* only sees records
emitted directly to that logger; records from child loggers propagate to the
parent's *handlers* but bypass the parent logger's *filters* — child records are
not auto-covered. A ``Filter`` attached to a *handler*, by
contrast, runs for every record that handler emits, including propagated ones.
:func:`install_secret_redaction` therefore attaches the filter to the root logger
AND to every handler currently configured anywhere in the logging tree.

What is redacted (a small set of compiled patterns — kept cheap)
-----------------------------------------------------------------
* URL userinfo — ``scheme://...@host`` → ``scheme://<redacted>@host`` (credentials before ``@`` are dropped).
* ``Authorization:`` / ``x-access-token:`` header values (any scheme/value).
* ``Basic``/``Bearer`` credential blobs (the base64 the runner builds is a
  ``Basic <base64>``; a remote can reflect the header value back in a response).
* Well-known token shapes by prefix (``ghp_``/``github_pat_``/``glpat-``/
  ``xoxb-``/``sk-`` …) — these are unambiguous and high-signal.

What is deliberately NOT redacted
---------------------------------
Context-free high-entropy blobs (a bare 40-hex string, a base64 payload with no
credential marker around it) are left alone. AKB logs git object SHAs, vault ids,
and content digests constantly; blanket-redacting every long hex/base64 run would
gut legitimate diagnostics and violates the "normal messages unaffected"
acceptance criterion. High-entropy secrets are caught in *credential context*
(after ``Authorization``/``Basic``/``Bearer``/``x-access-token``/a known prefix);
the runner's source-level ``_sanitize`` remains the precise tool for the mirror
path. Exception *tracebacks* are covered too: a string already cached on the
record (``exc_text``) is scrubbed in place, and the common ``logger.exception()``
record — ``exc_info`` set but ``exc_text`` not rendered until the handler's
formatter runs, i.e. AFTER this filter — has its traceback formatted, redacted,
and cached here so a secret in the exception message cannot slip past unredacted.
The render cost falls only on records that actually carry an exception; the
benign steady-state record is untouched.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

__all__ = ["SecretRedactingFilter", "install_secret_redaction", "redact"]

# ── Redaction patterns (few, compiled once — "정규식 소수·컴파일") ──
# Each entry is (compiled_regex, replacement). Applied in order.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # URL userinfo: strip the whole authority-userinfo run up to the LAST '@'
    # still inside the authority (``[^/\s?#]`` excludes the path/query/fragment
    # and whitespace so the match cannot cross into the path or a following URL).
    # Mirrors the runner's own sanitizer so both agree on the shape.
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s?#]+@"), r"\1<redacted>@"),
    # `Authorization: <anything>` / `Authorization=<anything>` — redact the value
    # up to a line/field delimiter. Case-insensitive; covers Basic, Bearer, token,
    # or an opaque value in one sweep.
    (re.compile(r"(?i)(authorization)\s*[:=]\s*[^\r\n,;]+"), r"\1: <redacted>"),
    # `x-access-token: <value>` / `x-access-token=<value>` header form. Like
    # Authorization, redact the whole value up to a line/field delimiter (NOT the
    # first whitespace) so a value that itself contains spaces — e.g. a reflected
    # `Basic <b64>` sitting after the header name — is removed in full instead of
    # leaving its tail exposed.
    (re.compile(r"(?i)(x-access-token)\s*[:=]\s*[^\r\n,;]+"), r"\1: <redacted>"),
    # A `Basic`/`Bearer` credential blob NOT necessarily behind an Authorization
    # label (e.g. a bare `Basic <b64>` reflected in an error body). Redact a
    # token-shaped run of >= 16 chars regardless of character class, so an
    # all-alphabetic Bearer value is caught too (a non-letter is no longer
    # required). The length floor is what keeps English prose out: a single
    # unbroken 16+ char run does not occur in phrases like "Basic authentication"
    # / "Bearer token required" (each word is shorter and space-separated), so
    # those are still left untouched.
    (
        re.compile(r"(?i)\b(basic|bearer)\s+[A-Za-z0-9+/=_.\-]{16,}"),
        r"\1 <redacted>",
    ),
    # Well-known token prefixes (GitHub / GitLab / Slack / OpenAI-style). The
    # prefix is unambiguous, so redact the whole opaque tail.
    (
        re.compile(
            r"\b(gh[pousr]_|github_pat_|glpat-|xox[baprs]-|sk-)[A-Za-z0-9_\-]{8,}"
        ),
        r"\1<redacted>",
    ),
)

# Cheap gate: if a rendered message matches NONE of these markers it cannot
# contain any secret the patterns above target, so we skip the full sweep (and
# skip rendering ``msg % args`` for the common benign record). One compiled
# alternation, run once.
_TRIGGER = re.compile(
    r"(?i)://|@|authorization|x-access-token|\bbasic\s|\bbearer\s|"
    r"gh[pousr]_|github_pat_|glpat-|xox[baprs]-|\bsk-"
)


def redact(text: str) -> str:
    """Return ``text`` with every configured secret pattern redacted.

    Pure and idempotent (redacting an already-redacted string is a no-op). Safe
    on any string; non-str callers should coerce first.
    """
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _arg_may_hold_secret(args: object) -> bool:
    """Cheap check of a record's ``args`` for a trigger marker without rendering
    the full message. Handles the tuple form and the ``%(name)s`` mapping form."""
    try:
        values: object
        if isinstance(args, Mapping):
            values = args.values()
        elif isinstance(args, tuple):
            values = args
        else:  # a single non-tuple arg (logging wraps it, but be defensive)
            values = (args,)
        for value in values:  # type: ignore[union-attr]
            if _TRIGGER.search(str(value)):
                return True
    except Exception:  # noqa: BLE001 — a broken __str__ must never break logging
        return False
    return False


# A format-string-independent formatter, reused to render tracebacks we need to
# redact before the handler's own formatter does. ``formatException`` uses no
# shared mutable state (a fresh StringIO per call), so one instance is safe to
# share across threads.
_EXC_FORMATTER = logging.Formatter()


class SecretRedactingFilter(logging.Filter):
    """A ``logging.Filter`` that redacts secrets from a record in place.

    Returns True always (it never drops a record — it only rewrites it). To catch
    secrets that only appear after ``msg % args`` substitution, it renders the
    message and, when a redaction actually changes it, folds the rendered result
    into ``record.msg`` and clears ``record.args``. The common (benign) record is
    left untouched — including its lazy ``args`` formatting — after a cheap
    trigger check, so steady-state logging overhead stays negligible. Exception
    and stack text are scrubbed on every record (see ``_scrub_exc_and_stack``).
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 — logging API
        try:
            # Message/args sweep — only when the template or an arg carries a
            # trigger marker. The benign record skips the render entirely and
            # keeps its lazy ``args`` formatting.
            if _TRIGGER.search(str(record.msg)) or _arg_may_hold_secret(record.args):
                message = record.getMessage()
                redacted = redact(message)
                if redacted != message:
                    record.msg = redacted
                    record.args = None
            # Traceback/stack text can carry a secret even when the message is
            # clean (a URL or token inside the exception). Scrub it here, BEFORE
            # the handler's formatter renders it, so the emitted record is clean.
            self._scrub_exc_and_stack(record)
        except Exception:  # noqa: BLE001 — redaction must never break logging
            return True
        return True

    @staticmethod
    def _scrub_exc_and_stack(record: logging.LogRecord) -> None:
        """Redact secrets from the record's exception traceback and stack text.

        Two states for the traceback:

        * ``exc_text`` already cached (a handler formatted the record earlier) —
          redact it in place.
        * ``exc_info`` set but ``exc_text`` still empty — the usual state right
          after ``logger.exception()`` / ``exc_info=True``. The handler's
          ``Formatter`` renders the traceback LATER (after this filter runs), so
          a secret in the exception message would otherwise slip past. Render it
          now, redact it, and cache it in ``exc_text``; ``Formatter.format``
          reuses that redacted string verbatim instead of re-deriving one.

        ``stack_info`` is already a rendered string on the record — redact it in
        place. Every step is best-effort: traceback rendering must never break
        logging, so a failure to format leaves the record as-is.
        """
        exc_text = getattr(record, "exc_text", None)
        if exc_text:
            record.exc_text = redact(exc_text)
        elif record.exc_info:
            try:
                rendered = _EXC_FORMATTER.formatException(record.exc_info)
            except Exception:  # noqa: BLE001 — rendering must never break logging
                rendered = None
            if rendered:
                record.exc_text = redact(rendered)

        stack_info = getattr(record, "stack_info", None)
        if stack_info:
            record.stack_info = redact(stack_info)


# Process-lifetime singleton so repeated installs share one filter instance and
# ``if _FILTER not in handler.filters`` de-dupes cleanly.
_FILTER = SecretRedactingFilter()


def install_secret_redaction(filt: logging.Filter | None = None) -> None:
    """Install the secret-redaction filter process-wide (idempotent).

    Attaches ``filt`` (the module singleton by default) to the root logger and to
    every handler currently configured on the root logger and on every named
    logger in the logging tree. Safe to call more than once and at any point —
    call it early (right after ``logging.basicConfig``) to cover the root handler,
    and again after other frameworks (uvicorn) have installed their own handlers
    to cover those too. Handlers created AFTER the last call are not covered
    (Python has no global handler registry); in this app all handlers exist by
    the time the lifespan startup runs, which is the second install point.
    """
    f = filt or _FILTER

    def _add_to_handler(handler: logging.Handler) -> None:
        if f not in handler.filters:
            handler.addFilter(f)

    root = logging.getLogger()
    if f not in root.filters:
        root.addFilter(f)
    for handler in list(root.handlers):
        _add_to_handler(handler)

    # Walk every configured logger and cover its own handlers. A logger with
    # ``propagate=False`` (e.g. uvicorn.access) never reaches the root handler, so
    # its handlers must be covered directly.
    manager = logging.Logger.manager
    for logger_obj in list(manager.loggerDict.values()):
        if not isinstance(logger_obj, logging.Logger):
            continue  # PlaceHolder entries have no handlers
        for handler in list(logger_obj.handlers):
            _add_to_handler(handler)
