"""Unit test for the publication owner-bypass allowlist (G5 / publish-hardening).

`_is_publication_manager` gates the password-throttle bypass. It must grant the
bypass ONLY to a REAL manager (role_source in {member, system_admin,
write_policy_admin_bypass}) — NOT to a user who merely passes the writer check
via a vault's ``public_access="writer"`` (role_source="public"), which would
silently disable brute-force protection for that whole class of publication.
Also fail-closed on anonymous / unknown-slug / access errors.

DB-free: the three awaited dependencies are monkeypatched. `pytest -k 'not _e2e'`.
"""
from types import SimpleNamespace

from app.api.routes import public
from app.exceptions import ForbiddenError, NotFoundError

_USER = SimpleNamespace(user_id="u1")
_PUB = {"vault": "v1"}


def _patch(monkeypatch, *, user, pub, access=None, access_exc=None):
    async def fake_user(request):
        return user

    async def fake_get_pub(slug):
        return pub

    async def fake_access(uid, vault, required_role="writer"):
        if access_exc is not None:
            raise access_exc
        return access

    monkeypatch.setattr(public, "get_optional_user", fake_user)
    monkeypatch.setattr(public.publication_service, "get_publication_by_slug", fake_get_pub)
    monkeypatch.setattr(public, "check_vault_access", fake_access)


async def test_real_member_gets_bypass(monkeypatch):
    _patch(monkeypatch, user=_USER, pub=_PUB, access={"role_source": "member"})
    assert await public._is_publication_manager(object(), "slug") is True


async def test_system_admin_gets_bypass(monkeypatch):
    _patch(monkeypatch, user=_USER, pub=_PUB, access={"role_source": "system_admin"})
    assert await public._is_publication_manager(object(), "slug") is True


async def test_public_writer_does_not_bypass(monkeypatch):
    # THE security property: passing the writer check via a public_access="writer"
    # vault (role_source="public") must NOT grant the throttle bypass.
    _patch(monkeypatch, user=_USER, pub=_PUB, access={"role_source": "public"})
    assert await public._is_publication_manager(object(), "slug") is False


async def test_anonymous_does_not_bypass(monkeypatch):
    _patch(monkeypatch, user=None, pub=_PUB, access={"role_source": "member"})
    assert await public._is_publication_manager(object(), "slug") is False


async def test_forbidden_fails_closed(monkeypatch):
    _patch(monkeypatch, user=_USER, pub=_PUB, access_exc=ForbiddenError("nope"))
    assert await public._is_publication_manager(object(), "slug") is False


async def test_unknown_slug_does_not_bypass(monkeypatch):
    _patch(monkeypatch, user=_USER, pub=None, access={"role_source": "member"})
    assert await public._is_publication_manager(object(), "slug") is False


async def test_notfound_fails_closed(monkeypatch):
    _patch(monkeypatch, user=_USER, pub=_PUB, access_exc=NotFoundError("Vault", "v1"))
    assert await public._is_publication_manager(object(), "slug") is False
