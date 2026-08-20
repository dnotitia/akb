"""The arrival ``invite_only`` refuses, and what an administrator can do with it.

Every one of these runs against a real PostgreSQL because the object under test
is a row with a uniqueness constraint, a retention window, and a cap that
evicts. A mock would agree with whatever this file assumed.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from app.config import settings
from app.exceptions import (
    ExternalIdentityAdoptionNotRequestedError,
    ExternalIdentityConflictError,
    ExternalIdentityIssuerMismatchError,
    MembershipRequiredError,
    NotFoundError,
)


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_ISSUER = "https://id.example.com/realms/akb"


@pytest.fixture
async def services(monkeypatch):
    try:
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3)
    except Exception:
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    from app.db.postgres import _load_migration
    from app.services import account_service, admission_service, auth_service

    async with pool.acquire() as conn:
        for filename in (
            "043_workspace_account_governance.py",
            "082_pending_admissions.py",
        ):
            migration = _load_migration(filename)
            assert migration is not None
            await migration.migrate(conn=conn)
        await conn.execute("DELETE FROM pending_admissions WHERE issuer = $1", _ISSUER)

    async def _get_pool():
        return pool

    monkeypatch.setattr(auth_service, "get_pool", _get_pool)
    monkeypatch.setattr(account_service, "get_pool", _get_pool)
    monkeypatch.setattr(admission_service, "get_pool", _get_pool)
    monkeypatch.setattr(account_service, "get_role_sync", lambda: _NoRoleSync())
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://id.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)
    monkeypatch.setattr(settings, "keycloak_require_verified_email", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_link_by_email", False, raising=False)
    assert settings.keycloak_issuer == _ISSUER

    yield pool, auth_service, admission_service, account_service

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM pending_admissions WHERE issuer LIKE 'https://id.example%'")
        await conn.execute("DELETE FROM users WHERE username LIKE 'adm-%'")
        await conn.execute("DELETE FROM events WHERE actor_id LIKE 'adm-%'")
    await pool.close()


class _NoRoleSync:
    async def on_user_create(self, user_id):
        return None


def _claims(subject: str, email: str | None = None, name: str | None = None) -> dict:
    claims: dict[str, object] = {
        "iss": _ISSUER,
        "sub": subject,
        "preferred_username": f"adm-{subject}",
    }
    if email is not None:
        claims.update({"email": email, "email_verified": True})
    if name is not None:
        claims["name"] = name
    return claims


async def _arrival(pool, subject: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM pending_admissions WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            subject,
        )


# ── the refusal now leaves a record, and is still a refusal ──────────────────


async def test_invite_only_records_the_exact_arrival_it_refuses(services):
    pool, auth_service, _, _ = services
    subject = f"arrival-{uuid.uuid4().hex}"
    email = f"adm-{uuid.uuid4().hex[:10]}@example.com"

    with pytest.raises(MembershipRequiredError) as refusal:
        await auth_service._resolve_or_provision_keycloak_user(
            _claims(subject, email, name="Invited Person")
        )

    # The refusal is unchanged. A note is not a way in.
    assert refusal.value.code == "membership_required"
    assert refusal.value.status_code == 403

    row = await _arrival(pool, subject)
    assert row is not None, "the subject the broker just minted was discarded again"
    assert row["issuer"] == _ISSUER
    assert row["subject"] == subject
    assert row["email"] == email
    assert row["display_name"] == "Invited Person"
    assert row["arrivals"] == 1


async def test_a_person_who_keeps_trying_stays_one_row(services):
    pool, auth_service, _, _ = services
    subject = f"repeat-{uuid.uuid4().hex}"
    email = f"adm-{uuid.uuid4().hex[:10]}@example.com"

    for _ in range(3):
        with pytest.raises(MembershipRequiredError):
            await auth_service._resolve_or_provision_keycloak_user(_claims(subject, email))

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM pending_admissions WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            subject,
        )
    assert len(rows) == 1
    assert rows[0]["arrivals"] == 3
    assert rows[0]["last_seen_at"] >= rows[0]["first_seen_at"]


async def test_two_arrivals_sharing_an_address_are_two_rows(services):
    """Nothing here is keyed by email — that is the whole point of the design."""
    pool, auth_service, _, _ = services
    email = f"adm-shared-{uuid.uuid4().hex[:10]}@example.com"
    first = f"shared-a-{uuid.uuid4().hex}"
    second = f"shared-b-{uuid.uuid4().hex}"

    for subject in (first, second):
        with pytest.raises(MembershipRequiredError):
            await auth_service._resolve_or_provision_keycloak_user(_claims(subject, email))

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM pending_admissions WHERE email = $1", email
        )
    assert count == 2


async def test_open_mode_records_nothing(services):
    pool, auth_service, _, _ = services
    monkeypatch_subject = f"open-{uuid.uuid4().hex}"
    settings_mode = settings.keycloak_enrollment_mode
    try:
        object.__setattr__(settings, "keycloak_enrollment_mode", "open")
        resolved = await auth_service._resolve_or_provision_keycloak_user(
            _claims(monkeypatch_subject, f"adm-{uuid.uuid4().hex[:10]}@example.com")
        )
    finally:
        object.__setattr__(settings, "keycloak_enrollment_mode", settings_mode)
    assert resolved["newly_provisioned"] is True
    assert await _arrival(pool, monkeypatch_subject) is None


async def test_a_note_that_cannot_be_written_is_still_a_refusal(services):
    """Taking a note must never be able to change the answer — in either direction.

    The recorder swallows its own failure on purpose, so this asserts the two
    halves that makes acceptable: the caller still gets the refusal, and the
    swallow is not hiding a partial write.
    """
    pool, auth_service, admission_service, _ = services
    subject = f"broken-{uuid.uuid4().hex}"

    async def _explode(conn, **kwargs):
        raise RuntimeError("pending_admissions is unreachable")

    original = auth_service.record_arrival
    auth_service.record_arrival = _explode  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            await auth_service._resolve_or_provision_keycloak_user(_claims(subject))
    finally:
        auth_service.record_arrival = original  # type: ignore[assignment]

    # And the recorder itself, given a connection that fails, refuses to raise.
    class _FailingConn:
        def transaction(self):
            raise RuntimeError("no transaction for you")

    await admission_service.record_arrival(
        _FailingConn(), issuer=_ISSUER, subject=subject, claims=_claims(subject)
    )
    assert await _arrival(pool, subject) is None


# ── the record is bounded, and both bounds keep the newest ───────────────────


async def test_an_arrival_past_its_retention_window_is_evicted_by_the_next_one(services):
    pool, auth_service, admission_service, _ = services
    stale = f"stale-{uuid.uuid4().hex}"
    fresh = f"fresh-{uuid.uuid4().hex}"

    with pytest.raises(MembershipRequiredError):
        await auth_service._resolve_or_provision_keycloak_user(_claims(stale))
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE pending_admissions SET last_seen_at = NOW() - interval '400 hours'"
            " WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            stale,
        )
    with pytest.raises(MembershipRequiredError):
        await auth_service._resolve_or_provision_keycloak_user(_claims(fresh))

    assert await _arrival(pool, stale) is None
    assert await _arrival(pool, fresh) is not None
    listed = await admission_service.list_pending_admissions()
    assert stale not in [item["subject"] for item in listed["pending_admissions"]]


async def test_the_cap_evicts_the_least_recent_and_never_the_newest(services):
    pool, auth_service, _, _ = services
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM pending_admissions")
    object.__setattr__(settings, "keycloak_pending_admission_cap", 3)
    try:
        subjects = [f"cap-{index}-{uuid.uuid4().hex[:8]}" for index in range(5)]
        for subject in subjects:
            with pytest.raises(MembershipRequiredError):
                await auth_service._resolve_or_provision_keycloak_user(_claims(subject))
    finally:
        object.__setattr__(
            settings,
            "keycloak_pending_admission_cap",
            type(settings).model_fields["keycloak_pending_admission_cap"].default,
        )

    async with pool.acquire() as conn:
        remaining = [
            row["subject"]
            for row in await conn.fetch(
                "SELECT subject FROM pending_admissions ORDER BY last_seen_at DESC, id"
            )
        ]
    assert len(remaining) == 3
    assert subjects[-1] in remaining
    assert subjects[0] not in remaining


async def test_an_evicted_arrival_comes_back_by_arriving_again(services):
    """Why eviction is the safe side of the capacity question.

    Refusing to record at capacity would be unrecoverable — one flood and no
    new arrival could ever be noted until a human pruned, so admission itself
    would be denied. Eviction loses only what re-arriving restores, and this
    asserts that it does.
    """
    pool, auth_service, _, _ = services
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM pending_admissions")
    evicted = f"evicted-{uuid.uuid4().hex}"
    object.__setattr__(settings, "keycloak_pending_admission_cap", 2)
    try:
        for subject in (evicted, f"later-a-{uuid.uuid4().hex[:8]}", f"later-b-{uuid.uuid4().hex[:8]}"):
            with pytest.raises(MembershipRequiredError):
                await auth_service._resolve_or_provision_keycloak_user(_claims(subject))
        assert await _arrival(pool, evicted) is None

        with pytest.raises(MembershipRequiredError):
            await auth_service._resolve_or_provision_keycloak_user(_claims(evicted))
        assert await _arrival(pool, evicted) is not None
    finally:
        object.__setattr__(
            settings,
            "keycloak_pending_admission_cap",
            type(settings).model_fields["keycloak_pending_admission_cap"].default,
        )


# ── approval ─────────────────────────────────────────────────────────────────


async def _record_and_read(services, subject: str, email: str) -> dict:
    _, auth_service, admission_service, _ = services
    with pytest.raises(MembershipRequiredError):
        await auth_service._resolve_or_provision_keycloak_user(_claims(subject, email))
    listed = await admission_service.list_pending_admissions()
    match = [item for item in listed["pending_admissions"] if item["subject"] == subject]
    assert len(match) == 1
    return match[0]


async def test_approval_binds_that_exact_arrival_and_the_person_gets_in(services):
    pool, auth_service, admission_service, _ = services
    subject = f"approve-{uuid.uuid4().hex}"
    email = f"adm-{uuid.uuid4().hex[:10]}@example.com"
    arrival = await _record_and_read(services, subject, email)

    result = await admission_service.approve_pending_admission(
        arrival["id"], actor_id="adm-actor"
    )
    user_id = result["user"]["user_id"]

    # The same login predicate that was refused now resolves — to that account.
    resolved = await auth_service._resolve_or_provision_keycloak_user(_claims(subject, email))
    assert str(resolved["user_id"]) == user_id
    assert resolved["newly_provisioned"] is False

    # And the note is gone, so the list an administrator reads is not a backlog
    # of people who are already in.
    assert await _arrival(pool, subject) is None


async def test_approval_can_attach_the_arrival_to_the_account_they_already_had(services):
    """The migration case: a new issuer, the same person, the same AKB account."""
    pool, auth_service, admission_service, _ = services
    subject = f"attach-{uuid.uuid4().hex}"
    email = f"adm-{uuid.uuid4().hex[:10]}@example.com"
    existing = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, auth_provider,
                               account_status, account_kind)
            VALUES ($1, $2, $3, '!keycloak-sso:no-local-login!', 'keycloak', 'active', 'human')
            """,
            existing,
            f"adm-{uuid.uuid4().hex[:12]}",
            email,
        )
    arrival = await _record_and_read(services, subject, email)

    result = await admission_service.approve_pending_admission(
        arrival["id"], actor_id="adm-actor", existing_user_id=str(existing)
    )
    assert result["user"]["user_id"] == str(existing)
    resolved = await auth_service._resolve_or_provision_keycloak_user(_claims(subject, email))
    assert str(resolved["user_id"]) == str(existing)


async def test_approval_cannot_name_a_subject_only_a_row(services):
    """The request model is the guard: naming a row IS the act of approval.

    Asserted as an exact field set rather than as two absences, so a later
    `issuer` or `subject` cannot appear here without this failing.
    """
    from app.api.routes.access import ApprovePendingAdmissionRequest

    with pytest.raises(Exception) as rejected:
        ApprovePendingAdmissionRequest(subject="someone-elses-subject")
    assert "extra" in str(rejected.value).lower()
    assert set(ApprovePendingAdmissionRequest.model_fields) == {
        "existing_user_id",
        "email",
        "display_name",
        "prepare_suspended",
    }


async def test_approval_never_picks_an_account_by_address(services):
    """The address on an arrival is a claim out of the person's own token.

    Letting it select an existing account would make approval an adoption by
    address at the one step that exists to prevent that. The refusal names the
    candidate, so an administrator who does mean that account can say so.
    """
    pool, _, admission_service, _ = services
    subject = f"noadopt-{uuid.uuid4().hex}"
    email = f"adm-{uuid.uuid4().hex[:10]}@example.com"
    squatter = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, auth_provider,
                               account_status, account_kind)
            VALUES ($1, $2, $3, '!keycloak-sso:no-local-login!', 'keycloak', 'active', 'human')
            """,
            squatter,
            f"adm-{uuid.uuid4().hex[:12]}",
            email,
        )
    arrival = await _record_and_read(services, subject, email)

    with pytest.raises(ExternalIdentityAdoptionNotRequestedError) as refused:
        await admission_service.approve_pending_admission(
            arrival["id"], actor_id="adm-actor"
        )
    assert refused.value.status_code == 409
    assert refused.value.details["candidate_user_id"] == str(squatter)

    # The arrival survives, and naming the account deliberately is allowed.
    assert await _arrival(pool, subject) is not None
    result = await admission_service.approve_pending_admission(
        arrival["id"], actor_id="adm-actor", existing_user_id=str(squatter)
    )
    assert result["user"]["user_id"] == str(squatter)


async def test_the_control_plane_path_still_adopts_an_unbound_address(services):
    """The default is unchanged: a caller that already identified the person.

    Only approval opts out. Asserting this here keeps the opt-out from being
    quietly widened into every caller.
    """
    pool, _, _, account_service = services
    email = f"adm-{uuid.uuid4().hex[:10]}@example.com"
    existing = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, auth_provider,
                               account_status, account_kind)
            VALUES ($1, $2, $3, '!keycloak-sso:no-local-login!', 'keycloak', 'active', 'human')
            """,
            existing,
            f"adm-{uuid.uuid4().hex[:12]}",
            email,
        )
    result = await account_service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=f"cp-{uuid.uuid4().hex}",
        email=email,
        display_name=None,
        actor_id="adm-actor",
    )
    assert result["user_id"] == str(existing)


async def test_an_arrival_under_a_foreign_issuer_is_refused_and_survives(services):
    pool, _, admission_service, _ = services
    subject = f"foreign-{uuid.uuid4().hex}"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO pending_admissions (issuer, subject, email)
            VALUES ($1, $2, $3) RETURNING id
            """,
            "https://id.example.com/realms/somewhere-else",
            subject,
            f"adm-{uuid.uuid4().hex[:10]}@example.com",
        )

    with pytest.raises(ExternalIdentityIssuerMismatchError):
        await admission_service.approve_pending_admission(str(row["id"]), actor_id="adm-actor")

    async with pool.acquire() as conn:
        still = await conn.fetchval(
            "SELECT count(*) FROM pending_admissions WHERE id = $1", row["id"]
        )
    assert still == 1, "a failed approval must leave the arrival where it was"


async def test_approving_one_arrival_onto_someone_elses_account_conflicts(services):
    pool, _, admission_service, _ = services
    subject = f"conflict-{uuid.uuid4().hex}"
    email = f"adm-{uuid.uuid4().hex[:10]}@example.com"
    arrival = await _record_and_read(services, subject, email)
    other = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, auth_provider,
                               account_status, account_kind)
            VALUES ($1, $2, $3, '!keycloak-sso:no-local-login!', 'keycloak', 'active', 'human')
            """,
            other,
            f"adm-{uuid.uuid4().hex[:12]}",
            f"adm-{uuid.uuid4().hex[:10]}@example.com",
        )
    await admission_service.approve_pending_admission(arrival["id"], actor_id="adm-actor")

    # The row is gone, so a second approval has nothing to name.
    with pytest.raises(NotFoundError):
        await admission_service.approve_pending_admission(arrival["id"], actor_id="adm-actor")

    # And re-arriving after the binding exists produces no new note to approve.
    async with pool.acquire() as conn:
        replay = await conn.fetchrow(
            """
            INSERT INTO pending_admissions (issuer, subject, email)
            VALUES ($1, $2, $3) RETURNING id
            """,
            _ISSUER,
            subject,
            email,
        )
    with pytest.raises(ExternalIdentityConflictError):
        await admission_service.approve_pending_admission(
            str(replay["id"]), actor_id="adm-actor", existing_user_id=str(other)
        )


async def test_a_binding_written_by_any_path_answers_the_note(services):
    """A control plane that prelinks someone who had already been turned away."""
    pool, auth_service, _, account_service = services
    subject = f"prelinked-{uuid.uuid4().hex}"
    email = f"adm-{uuid.uuid4().hex[:10]}@example.com"
    with pytest.raises(MembershipRequiredError):
        await auth_service._resolve_or_provision_keycloak_user(_claims(subject, email))
    assert await _arrival(pool, subject) is not None

    await account_service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=subject,
        email=email,
        display_name=None,
        actor_id="adm-actor",
    )
    assert await _arrival(pool, subject) is None


async def test_dismissing_an_arrival_forgets_it_without_admitting_anyone(services):
    pool, auth_service, admission_service, _ = services
    subject = f"dismiss-{uuid.uuid4().hex}"
    email = f"adm-{uuid.uuid4().hex[:10]}@example.com"
    arrival = await _record_and_read(services, subject, email)

    await admission_service.dismiss_pending_admission(arrival["id"])
    assert await _arrival(pool, subject) is None
    with pytest.raises(MembershipRequiredError):
        await auth_service._resolve_or_provision_keycloak_user(_claims(subject, email))
    with pytest.raises(NotFoundError):
        await admission_service.dismiss_pending_admission(arrival["id"])


async def test_the_boundary_carries_the_reason_the_service_layer_raised(services):
    """"Refused" and "refused because they are not a member" are different facts.

    The projection boundary answered None for every rejection there is, so the
    HTTP layer could not tell them apart and generalised — which is how a person
    waiting for approval came to be told their token was invalid. The reason is
    carried now, and this is the test that fails if it stops being.
    """
    pool, auth_service, admission_service, _accounts = services
    from app.services.auth_verifier_profiles import VerifiedPrincipal

    email = "adm-reason@example.com"
    claims = _claims("subject-reason", email)
    claims["scope"] = "openid profile email"
    principal = VerifiedPrincipal(
        profile_id=auth_service.KEYCLOAK_ACCESS_V1,
        issuer=_ISSUER,
        subject="subject-reason",
        credential_type="bearer",
        claims=claims,
        audience="api",
    )

    outcome = await auth_service.project_verified_principal_with_reason(principal)
    assert outcome.user is None
    assert outcome.refusal_code == "membership_required"

    # The same arrival is on the administrator's list, so the reason the
    # boundary reports and the row they act on are about one event.
    listed = await admission_service.list_pending_admissions()
    rows = [r for r in listed["pending_admissions"] if r["subject"] == "subject-reason"]
    assert len(rows) == 1

    # Admitted, the same principal projects to an account and reports no reason.
    await admission_service.approve_pending_admission(
        rows[0]["id"], actor_id="adm-admin"
    )
    admitted = await auth_service.project_verified_principal_with_reason(principal)
    assert admitted.user is not None
    assert admitted.refusal_code is None

    # And the plain boundary still answers exactly what it always did.
    assert (await auth_service.project_verified_principal(principal)) is not None
