from __future__ import annotations

import hashlib
import uuid

import pytest

from app.exceptions import ValidationError
from app.services.m1_native_grep_service import M1NativeGrepService
from app.services.m1_pg_body_store import M1PgBodyStore, PgBodyIntegrityError


def _row(body: bytes = b"hello\nneedle\n") -> dict:
    return {
        "canonical_bytes": body,
        "byte_size": len(body),
        "digest": hashlib.sha256(body).hexdigest(),
        "encoding": "utf-8",
        "selected_placement": "pg-bodystore-v1",
        "verification_profile": "sha256-size-utf8-v1",
    }


def test_pg_body_candidate_verifies_one_canonical_utf8_representation():
    row = _row()
    assert M1PgBodyStore._verify_row(row) == row["canonical_bytes"]
    assert M1PgBodyStore._verified_bytes("hello") == (
        b"hello",
        hashlib.sha256(b"hello").hexdigest(),
    )


@pytest.mark.parametrize("payload", [b"bad\x00text", b"\xff"])
def test_pg_body_candidate_rejects_non_searchable_bytes(payload: bytes):
    with pytest.raises(ValidationError):
        M1PgBodyStore._verified_bytes(payload)


def test_pg_body_candidate_rejects_manifest_drift():
    row = _row()
    row["digest"] = "0" * 64
    with pytest.raises(PgBodyIntegrityError, match="digest"):
        M1PgBodyStore._verify_row(row)


def test_pg_body_receipt_binds_exact_verified_bytes():
    row = {
        **_row(b"hello"),
        "payload_id": uuid.uuid4(),
        "namespace_id": uuid.uuid4(),
        "content_profile": "text",
    }

    receipt = M1PgBodyStore._receipt_from_row(row)

    assert receipt.payload_id == row["payload_id"]
    assert receipt.digest == row["digest"]
    assert receipt.byte_size == 5
    assert receipt.canonical_bytes == b"hello"


def test_native_grep_matcher_has_literal_regex_and_case_contract():
    insensitive = M1NativeGrepService._matcher("needle", regex=False, case_sensitive=False)
    sensitive = M1NativeGrepService._matcher("Needle", regex=False, case_sensitive=True)
    regex = M1NativeGrepService._matcher(r"need(le|ful)", regex=True, case_sensitive=False)

    assert insensitive("NEEDLE")
    assert sensitive("Needle")
    assert not sensitive("needle")
    assert regex("Needful")
    with pytest.raises(ValidationError, match="Invalid regex"):
        M1NativeGrepService._matcher("(", regex=True, case_sensitive=False)


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _Conn:
    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.params = ()

    async def fetch(self, sql, *params):
        self.sql = sql
        self.params = params
        return [self.row]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_head_body_query_intersects_acl_and_pins_current_revision():
    body = b"needle\n"
    resource_id = uuid.uuid4()
    row = {
        **_row(body),
        "namespace_id": uuid.uuid4(),
        "vault": "engineering",
        "resource_id": resource_id,
        "surface": "file",
        "current_path": "src/main.py",
        "head_revision_id": "a" * 40,
    }
    conn = _Conn(row)
    service = M1NativeGrepService(_Pool(conn))

    bodies = await service._head_bodies(
        user_id=uuid.uuid4(),
        vaults=["engineering"],
        collection="src",
        resource_id=resource_id,
    )

    assert len(bodies) == 1
    assert bodies[0].revision_id == "a" * 40
    assert bodies[0].text == "needle\n"
    assert "rs.head_revision_id" in conn.sql
    assert "vault_access" in conn.sql
    assert "pg-bodystore-v1" in conn.sql
    assert "ESCAPE" in conn.sql
    assert conn.params[2:] == ("src", "src/%", resource_id)
