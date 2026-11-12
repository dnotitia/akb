"""A Native document's author name is looked up by an indexable predicate.

`created_by` holds either a user id or a username. Matching both with
`id::text = $1 OR username = $1` could use neither index, so it scanned
`users` on every document read. The lookup now picks one indexed equality per
form, and keeps the old answers: an id names its user, a username names its
user, a UUID-looking username still resolves, and an unknown token is None.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from app.services.native_document_service import NativeDocumentService, _canonical_user_id

_BY_ID = "SELECT COALESCE(display_name, username) FROM users WHERE id = $1"
_BY_USERNAME = "SELECT COALESCE(display_name, username) FROM users WHERE username = $1"


class _Users:
    """A `users` table answering only the two indexed lookups.

    It computes `COALESCE(display_name, username)` itself, so it accepts each
    lookup only in the exact form that selects that expression.
    """

    def __init__(self, *rows: tuple[uuid.UUID, str, str | None]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, object]] = []

    async def fetchval(self, query: str, value: object):
        self.queries.append((query, value))
        normalized = " ".join(query.split())
        if normalized == _BY_ID:
            assert isinstance(value, uuid.UUID)
            matches = [row for row in self.rows if row[0] == value]
        elif normalized == _BY_USERNAME:
            matches = [row for row in self.rows if row[1] == value]
        else:  # pragma: no cover - a new query shape must be reviewed here
            raise AssertionError(f"unexpected query: {normalized}")
        if not matches:
            return None
        _, username, display_name = matches[0]
        return display_name if display_name is not None else username


class _Pool:
    def __init__(self, conn: _Users) -> None:
        self.conn = conn
        self.acquired = 0

    @asynccontextmanager
    async def acquire(self):
        self.acquired += 1
        yield self.conn


def _service(*rows) -> tuple[NativeDocumentService, _Pool]:
    pool = _Pool(_Users(*rows))
    return NativeDocumentService(pool=pool), pool


async def test_a_user_id_is_resolved_by_primary_key_alone():
    user_id = uuid.uuid4()
    service, pool = _service((user_id, "alice", "Alice Kim"))

    assert await service._created_by_name(str(user_id)) == "Alice Kim"
    assert [value for _, value in pool.conn.queries] == [user_id]


async def test_an_id_without_a_display_name_resolves_to_the_username():
    user_id = uuid.uuid4()
    service, pool = _service((user_id, "alice", None))

    assert await service._created_by_name(str(user_id)) == "alice"
    assert pool.conn.queries == [(_BY_ID, user_id)]


@pytest.mark.parametrize(
    "user_id",
    [
        uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        uuid.uuid3(uuid.NAMESPACE_URL, "https://example.test/users/owner"),
        uuid.uuid5(uuid.NAMESPACE_URL, "https://example.test/users/owner"),
        uuid.UUID("01890a5d-ac96-774b-bcce-b302099a8057"),
    ],
    ids=lambda user_id: f"v{user_id.version}",
)
async def test_a_canonical_uuid_of_any_version_is_a_user_id(user_id):
    """`id::text` matched an id of any UUID version, not only random ones."""
    assert user_id.version != 4
    service, pool = _service((user_id, "owner", "Owner"))

    assert _canonical_user_id(str(user_id)) == user_id
    assert await service._created_by_name(str(user_id)) == "Owner"
    assert pool.conn.queries == [(_BY_ID, user_id)]


async def test_a_username_is_resolved_by_username_alone():
    service, pool = _service((uuid.uuid4(), "alice", None))

    assert await service._created_by_name("alice") == "alice"
    assert pool.conn.queries == [(_BY_USERNAME, "alice")]


async def test_a_uuid_shaped_username_still_resolves():
    lookalike = str(uuid.uuid4())
    service, pool = _service((uuid.uuid4(), lookalike, "Imported Bot"))

    assert await service._created_by_name(lookalike) == "Imported Bot"
    # The id lookup found nobody, so the username lookup answered.
    assert [value for _, value in pool.conn.queries] == [uuid.UUID(lookalike), lookalike]


async def test_an_id_match_wins_over_a_username_match():
    user_id = uuid.uuid4()
    service, pool = _service(
        (uuid.uuid4(), str(user_id), "Lookalike"),
        (user_id, "owner", "Owner"),
    )

    assert await service._created_by_name(str(user_id)) == "Owner"
    assert len(pool.conn.queries) == 1


async def test_an_unknown_token_is_none():
    service, pool = _service((uuid.uuid4(), "alice", "Alice"))

    assert await service._created_by_name("external-author") is None
    assert await service._created_by_name(str(uuid.uuid4())) is None
    assert len(pool.conn.queries) == 3


@pytest.mark.parametrize("created_by", [None, ""])
async def test_an_empty_token_reads_nothing(created_by):
    service, pool = _service()

    assert await service._created_by_name(created_by) is None
    assert pool.acquired == 0


@pytest.mark.parametrize(
    "token",
    [
        "0C37E906-6DB0-48C2-AC5D-576D0797B3F7",
        "0c37e9066db048c2ac5d576d0797b3f7",  # pragma: allowlist secret
        "{0c37e906-6db0-48c2-ac5d-576d0797b3f7}",
        "urn:uuid:0c37e906-6db0-48c2-ac5d-576d0797b3f7",
    ],
)
async def test_a_non_canonical_uuid_is_only_a_username(token):
    """`id::text` only ever produced the lowercase hyphenated form."""
    user_id = uuid.UUID("0c37e906-6db0-48c2-ac5d-576d0797b3f7")
    service, pool = _service((user_id, "owner", "Owner"))

    assert _canonical_user_id(token) is None
    assert await service._created_by_name(token) is None
    assert [value for _, value in pool.conn.queries] == [token]


@pytest.mark.parametrize("value", [123, 1.5, ["a"], {"a": 1}])
def test_a_non_string_is_never_a_user_id(value):
    assert _canonical_user_id(value) is None


def test_canonical_text_is_a_user_id():
    user_id = uuid.uuid4()
    assert _canonical_user_id(str(user_id)) == user_id
