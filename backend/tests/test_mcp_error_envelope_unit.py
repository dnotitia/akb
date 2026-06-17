"""Unit guard for dnotitia/akb#221.

The central MCP dispatch (`call_tool`) used to map every bubbled-up exception
to `code=internal`. Access guards (`check_vault_access`) raise `AKBError`
subclasses *outside* the per-handler try/except, so a permission denial or a
missing vault surfaced as a misleading `internal` (looks like a 500) instead of
the stable 4xx code it is. `_exception_envelope` now maps the known AKBError
subclasses to their canonical codes; everything else stays `internal`.
"""

from app.exceptions import (
    AKBError,
    AuthenticationError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.util.errors import (
    exception_envelope,
    CONFLICT,
    INTERNAL,
    INVALID_ARGUMENT,
    NOT_FOUND,
    PERMISSION_DENIED,
)


def test_forbidden_maps_to_permission_denied():
    # The #221 headline: a non-admin hitting an admin-gated tool.
    env = exception_envelope(ForbiddenError("Requires 'admin' role on vault 'v'"))
    assert env["code"] == PERMISSION_DENIED
    assert env["code"] != INTERNAL
    assert "admin" in env["error"]


def test_notfound_maps_to_not_found():
    env = exception_envelope(NotFoundError("Vault", "missing"))
    assert env["code"] == NOT_FOUND
    assert "missing" in env["error"]


def test_conflict_maps_to_conflict():
    assert exception_envelope(ConflictError("Table already exists: t"))["code"] == CONFLICT


def test_validation_maps_to_invalid_argument():
    assert exception_envelope(ValidationError("bad column"))["code"] == INVALID_ARGUMENT


def test_unknown_exception_stays_internal():
    assert exception_envelope(RuntimeError("boom"))["code"] == INTERNAL


def test_unmapped_akberror_subclass_stays_internal():
    # We only reclassify the explicitly-mapped subclasses — an AKBError type the
    # dispatch doesn't know about must NOT be silently relabelled as a 4xx.
    assert exception_envelope(AKBError("mystery"))["code"] == INTERNAL
    assert exception_envelope(AuthenticationError())["code"] == INTERNAL


def test_envelope_shape_is_canonical():
    env = exception_envelope(ForbiddenError("nope"))
    assert set(env.keys()) == {"error", "code"}
    assert env["error"] == "nope"
