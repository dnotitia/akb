"""`akb_create_table(if_not_exists=True)` — opt-in idempotent create.

Design: docs/design/proposal/2026-07-29-create-table-if-not-exists/

The load-bearing property is NOT "conflicts become successes" — it is that
exactly ONE conflict becomes a success. `ConflictError` is raised from four
sites in `create_table` and means two different things:

  * same vault, `(vault_id, name)` row exists      -> suppressible
  * a DIFFERENT vault's table fused onto the same
    physical name (issue #285)                     -> MUST stay a 409

Both raise the same exception type, so an implementation that catches
`ConflictError` broadly would hand a vault-A writer the schema of a vault-B
table. PostgreSQL's `information_schema` is privilege-filtered per vault role,
so that would be a genuine cross-tenant disclosure, not a restatement of
something the caller could already read.

DB-free: the fakes follow `test_vault_table_name_collision_unit`.
"""

from __future__ import annotations

import uuid

import pytest

from app.exceptions import ConflictError, ValidationError
from app.services import table_service

pytestmark = pytest.mark.asyncio

_COLS = [{"name": "title", "type": "text"}]


class _AsyncCtx:
    def __init__(self, value=None):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Drives create_table to a chosen decision point.

    `existing` is the row `find_by_name` should return (None = absent).
    `physical_taken` answers the cross-vault `to_regclass` preflight.
    Every `execute` is recorded so a test can assert the advisory lock was
    taken, and taken BEFORE the existence check.
    """

    def __init__(self, vault_name: str, *, existing=None, physical_taken=False):
        self._vault_name = vault_name
        self._existing = existing
        self._physical_taken = physical_taken
        self.executed: list[str] = []
        self.calls: list[str] = []

    def transaction(self, **kwargs):
        self.tx_kwargs = kwargs
        return _AsyncCtx()

    async def execute(self, sql: str, *params):
        self.executed.append(sql)
        self.calls.append(f"execute:{sql.split()[1] if len(sql.split()) > 1 else sql}")
        return "OK"

    async def fetchrow(self, sql: str, *params):
        if "FROM vaults" in sql:
            self.calls.append("vault_lookup")
            return {"name": self._vault_name}
        if "FROM vault_tables" in sql:
            self.calls.append("find_by_name")
            return self._existing
        return None

    async def fetchval(self, sql: str, *params):
        if "to_regclass" in sql:
            self.calls.append("to_regclass")
            return self._physical_taken
        if "pg_advisory" in sql:
            self.calls.append("advisory_lock")
            return None
        return None


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AsyncCtx(self._conn)


def _wire(monkeypatch, conn):
    async def _fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(table_service, "get_pool", _fake_get_pool)


def _stored_row(*, name="issues", columns=None, collection=None):
    """A `find_by_name` row as asyncpg would return it (dict-ified)."""
    return {
        "id": uuid.uuid4(),
        "vault_id": uuid.uuid4(),
        "collection_id": None,
        "collection": collection,
        "name": name,
        "description": "",
        "columns": columns if columns is not None else _COLS,
        "unique_keys": [],
        "indexes": [],
        "created_by": "someone",
        "created_at": None,
        "updated_at": None,
    }


def _norm(specs: list[dict]) -> list[dict]:
    """Normalize like a real create would, so a comparison test isolates the
    field under test instead of tripping on `type` (json -> jsonb)."""
    from app.repositories.table_data_repo import normalize_column_spec
    return [normalize_column_spec(c) for c in specs]


def _forbid_ddl(monkeypatch, why: str):
    async def _must_not_run(*a, **k):
        raise AssertionError(why)

    monkeypatch.setattr(
        table_service.table_data_repo, "create_dynamic_table", _must_not_run)


# ── 1. default is unchanged ──────────────────────────────────────


async def test_default_still_conflicts_on_existing_table(monkeypatch):
    """Regression guard: omitting the flag must behave exactly as before."""
    _wire(monkeypatch, _FakeConn("v", existing=_stored_row()))
    _forbid_ddl(monkeypatch, "no DDL on a duplicate")

    with pytest.raises(ConflictError) as ei:
        await table_service.create_table(
            uuid.uuid4(), "issues", _COLS, actor_id="t")
    assert "already exists" in str(ei.value)


async def test_explicit_false_still_conflicts(monkeypatch):
    _wire(monkeypatch, _FakeConn("v", existing=_stored_row()))
    _forbid_ddl(monkeypatch, "no DDL on a duplicate")

    with pytest.raises(ConflictError):
        await table_service.create_table(
            uuid.uuid4(), "issues", _COLS, actor_id="t", if_not_exists=False)


# ── 2. the no-op branch ──────────────────────────────────────────


async def test_existing_table_returns_created_false(monkeypatch):
    _wire(monkeypatch, _FakeConn("v", existing=_stored_row()))
    _forbid_ddl(monkeypatch, "a no-op must not create anything")

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t", if_not_exists=True, can_read_existing=True)

    assert out["created"] is False
    assert out["outcome"] == "already_exists"
    assert out["name"] == "issues"
    assert out["kind"] == "table"


async def test_no_op_emits_no_domain_event(monkeypatch):
    """A no-op performed no domain write, so it must emit no event."""
    _wire(monkeypatch, _FakeConn("v", existing=_stored_row()))
    _forbid_ddl(monkeypatch, "a no-op must not create anything")

    async def _must_not_emit(*a, **k):
        raise AssertionError("no table.create event on a no-op")

    monkeypatch.setattr(table_service, "emit_event", _must_not_emit)

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t", if_not_exists=True, can_read_existing=True)
    assert out["created"] is False


async def test_no_op_skips_post_commit_metadata_indexing(monkeypatch):
    """`index_table_metadata` runs after the create TX; a no-op created
    nothing, so re-indexing would overwrite stored metadata with the
    LOSING request's values."""
    _wire(monkeypatch, _FakeConn("v", existing=_stored_row()))
    _forbid_ddl(monkeypatch, "a no-op must not create anything")

    async def _must_not_index(*a, **k):
        raise AssertionError("no metadata indexing on a no-op")

    monkeypatch.setattr(table_service, "index_table_metadata", _must_not_index)

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t", if_not_exists=True, can_read_existing=True)
    assert out["created"] is False


# ── 3. divergence must be machine-explicit ───────────────────────


async def test_identical_schema_reports_match(monkeypatch):
    _wire(monkeypatch, _FakeConn("v", existing=_stored_row(columns=_COLS)))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t", if_not_exists=True, can_read_existing=True)

    assert out["matches_request"] is True
    assert out["mismatches"] == []


async def test_divergent_schema_names_the_differing_fields(monkeypatch):
    """A success-looking created=false must not let the caller assume its
    requested columns exist."""
    stored = _stored_row(columns=[{"name": "headline", "type": "text"}])
    _wire(monkeypatch, _FakeConn("v", existing=stored))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t", if_not_exists=True, can_read_existing=True)

    assert out["matches_request"] is False
    assert "columns" in out["mismatches"]
    # the STORED schema, not the request's
    assert out["columns"] == [{"name": "headline", "type": "text"}]


async def test_divergent_collection_is_reported(monkeypatch):
    stored = _stored_row(collection="other/place")
    _wire(monkeypatch, _FakeConn("v", existing=stored))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t",
        collection="specs", if_not_exists=True, can_read_existing=True)

    assert out["matches_request"] is False
    assert "collection" in out["mismatches"]
    assert out["collection"] == "other/place"


async def test_legacy_json_string_columns_are_normalised(monkeypatch):
    """Legacy rows store `columns` as a JSON string literal. Comparison
    must go through the repo parser, or every legacy table reports a
    spurious mismatch."""
    stored = _stored_row(columns='[{"name": "title", "type": "text"}]')
    _wire(monkeypatch, _FakeConn("v", existing=stored))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t", if_not_exists=True, can_read_existing=True)

    assert out["columns"] == _COLS
    assert out["matches_request"] is True


@pytest.mark.parametrize("noop_field", [
    {"required": False},
    {"unique": False},
    {"index": False},
    {"default": None},
    {"required": False, "unique": False, "index": False, "default": None},
])
async def test_no_op_falsy_flags_are_not_a_mismatch(monkeypatch, noop_field):
    """`required: false` and omitting `required` describe the SAME column, and
    normalization keeps whichever form the caller used. Comparing the raw
    normalized dicts therefore reports a mismatch between two identical
    schemas — purely because the stored row was created with one spelling and
    this request uses the other.

    A false mismatch is worse than no signal: it tells the caller to go alter
    a table that already matches.
    """
    stored = _stored_row(columns=[{"name": "title", "type": "text"}])
    _wire(monkeypatch, _FakeConn("v", existing=stored))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues",
        [{"name": "title", "type": "text", **noop_field}],
        actor_id="t", if_not_exists=True, can_read_existing=True)

    assert out["matches_request"] is True, (
        f"{noop_field} should not differ from omitting it; "
        f"mismatches={out['mismatches']}")


async def test_boolean_default_false_is_a_real_difference(monkeypatch):
    """`default: false` is NOT a no-op spelling.

    A boolean column with no default generates `BOOLEAN`; with
    `default: false` it generates `BOOLEAN DEFAULT FALSE`. Those are
    different tables. `required`/`unique`/`index` are absent-equivalent when
    false; `default` is only absent-equivalent when NULL.
    """
    stored = _stored_row(columns=[{"name": "flag", "type": "boolean"}])
    _wire(monkeypatch, _FakeConn("v", existing=stored))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues",
        [{"name": "flag", "type": "boolean", "default": False}],
        actor_id="t", if_not_exists=True, can_read_existing=True)

    assert out["matches_request"] is False, (
        "default=false vs no default are different tables")
    assert "columns" in out["mismatches"]


@pytest.mark.parametrize("stored_default,requested_default", [
    (False, 0),
    (0, False),
    (True, 1),
    (1, True),
])
async def test_bool_and_int_defaults_are_not_interchangeable(
    monkeypatch, stored_default, requested_default,
):
    """Python says `False == 0` and `True == 1`. JSONB does not, and neither
    does the generated DDL — `DEFAULT FALSE` is not `DEFAULT 0`. A plain `==`
    on the normalized dicts therefore HIDES a real divergence."""
    # Normalize the STORED side too, or the mismatch lands on `type`
    # (json -> jsonb) and the test passes without ever exercising the
    # default comparison.
    stored = _stored_row(columns=_norm(
        [{"name": "v", "type": "json", "default": stored_default}]))
    _wire(monkeypatch, _FakeConn("v", existing=stored))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues",
        [{"name": "v", "type": "json", "default": requested_default}],
        actor_id="t", if_not_exists=True, can_read_existing=True)

    assert out["matches_request"] is False, (
        f"stored default={stored_default!r} vs requested "
        f"{requested_default!r} must not compare equal")


async def test_nested_json_bool_vs_int_is_a_difference(monkeypatch):
    """The same trap one level down, inside a check spec."""
    stored = _stored_row(columns=_norm([
        {"name": "v", "type": "json", "check": {"op": "eq", "value": False}}]))
    _wire(monkeypatch, _FakeConn("v", existing=stored))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues",
        [{"name": "v", "type": "json", "check": {"op": "eq", "value": 0}}],
        actor_id="t", if_not_exists=True, can_read_existing=True)

    assert out["matches_request"] is False


@pytest.mark.parametrize("null_field", [
    {"check": None},
    {"enum": None},
    {"references": None},
    {"on_delete": None},
])
async def test_explicit_nulls_are_not_a_mismatch(monkeypatch, null_field):
    """REST accepts these as `None` (they are `X | None = None` on
    `TableColumnSpec`), and an explicit null is DDL-equivalent to omitting
    the field. Only `default` was being stripped, so these reported a
    spurious `columns` divergence."""
    stored = _stored_row(columns=[{"name": "title", "type": "text"}])
    _wire(monkeypatch, _FakeConn("v", existing=stored))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues",
        [{"name": "title", "type": "text", **null_field}],
        actor_id="t", if_not_exists=True, can_read_existing=True)

    assert out["matches_request"] is True, (
        f"explicit {null_field} should equal omitting it; "
        f"mismatches={out['mismatches']}")


async def test_a_real_column_difference_is_still_reported(monkeypatch):
    """Guard the guard: dropping no-op flags must not also drop real ones."""
    stored = _stored_row(columns=[{"name": "title", "type": "text"}])
    _wire(monkeypatch, _FakeConn("v", existing=stored))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues",
        [{"name": "title", "type": "text", "required": True}],
        actor_id="t", if_not_exists=True, can_read_existing=True)

    assert out["matches_request"] is False
    assert "columns" in out["mismatches"]


async def test_divergent_unique_keys_are_reported(monkeypatch):
    """`unique_keys` is returned in the envelope, so it must also be
    compared — otherwise a completely different constraint set reports
    matches_request=true, and the response lies about what exists."""
    stored = _stored_row()
    stored["unique_keys"] = [{"name": "uq_title", "columns": ["title"]}]
    _wire(monkeypatch, _FakeConn("v", existing=stored))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t",
        unique_keys=None, if_not_exists=True, can_read_existing=True)

    assert out["matches_request"] is False
    assert "unique_keys" in out["mismatches"]


async def test_divergent_indexes_are_reported(monkeypatch):
    stored = _stored_row()
    stored["indexes"] = [
        {"name": "ix_title", "columns": [{"name": "title", "order": "asc"}]}
    ]
    _wire(monkeypatch, _FakeConn("v", existing=stored))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t",
        indexes=None, if_not_exists=True, can_read_existing=True)

    assert out["matches_request"] is False
    assert "indexes" in out["mismatches"]


async def test_column_references_are_validated_on_the_no_op_path(monkeypatch):
    """A real create rejects a reference to a missing / non-unique / wrong-
    typed target. If the no-op skipped that check, the SAME request would be
    a 422 against an absent table and a quiet no-op against a present one —
    the flag would change what counts as a valid spec."""
    _wire(monkeypatch, _FakeConn("v", existing=_stored_row()))
    _forbid_ddl(monkeypatch, "no-op")

    async def _bad_ref(*a, **k):
        raise ValidationError("references a column that does not exist")

    monkeypatch.setattr(table_service, "_validate_column_references", _bad_ref)

    with pytest.raises(ValidationError):
        await table_service.create_table(
            uuid.uuid4(), "issues", _COLS, actor_id="t",
            if_not_exists=True, can_read_existing=True)


async def test_malformed_unique_keys_are_still_validated_on_the_no_op_path(
    monkeypatch,
):
    """"Ensure a table matching this spec" is not satisfiable by a spec
    that is not valid, so an existing table must not let a bad
    unique_keys/indexes payload through unchecked."""
    _wire(monkeypatch, _FakeConn("v", existing=_stored_row()))
    _forbid_ddl(monkeypatch, "no-op")

    with pytest.raises(ValidationError):
        await table_service.create_table(
            uuid.uuid4(), "issues", _COLS, actor_id="t",
            unique_keys=[{"columns": ["does_not_exist"]}], if_not_exists=True)


async def test_real_create_with_enum_column_is_not_double_normalized(monkeypatch):
    """`_normalize_column_spec` SYNTHESIZES a CHECK for an enum column, and
    then rejects a column that already carries one ("Enum columns derive
    their CHECK constraint from `enum`; omit `check`").

    So normalization is NOT idempotent, and the real create path must
    canonicalize exactly once. Sharing `_canonical_create_spec` between the
    two branches is what makes `matches_request` trustworthy — but it must
    not re-normalize columns the caller already normalized.
    """
    conn = _FakeConn("v", existing=None)
    _wire(monkeypatch, conn)

    async def _ok(*a, **k):
        return None

    monkeypatch.setattr(table_service.table_data_repo, "create_dynamic_table", _ok)
    monkeypatch.setattr(table_service.table_registry_repo, "insert", _ok)
    monkeypatch.setattr(table_service, "emit_event", _ok)
    monkeypatch.setattr(table_service, "index_table_metadata", _ok)
    monkeypatch.setattr(table_service, "_validate_column_references", _ok)

    class _RS:
        async def grant_table_in_conn(self, *a, **k):
            return None

    monkeypatch.setattr(table_service, "get_role_sync", lambda: _RS())

    out = await table_service.create_table(
        uuid.uuid4(), "issues",
        [{"name": "state", "type": "enum", "enum": ["todo", "done"]}],
        actor_id="t",
    )
    assert out["created"] is True


# ── 4. SECURITY: the flag must not cross a vault boundary ────────


async def test_cross_vault_fusion_still_conflicts_under_if_not_exists(monkeypatch):
    """#285 fusion: `find_by_name` finds NOTHING in this vault, but the
    physical name belongs to another vault's table. `if_not_exists=True`
    must NOT convert that into created=false."""
    _wire(monkeypatch, _FakeConn("a", existing=None, physical_taken=True))
    _forbid_ddl(monkeypatch, "must not reach DDL when the physical name is taken")

    with pytest.raises(ConflictError) as ei:
        await table_service.create_table(
            uuid.uuid4(), "b__c", _COLS, actor_id="t", if_not_exists=True)

    msg = str(ei.value)
    assert "another vault" in msg


async def test_cross_vault_fusion_leaks_no_schema(monkeypatch):
    """The 409 body must not carry the other tenant's columns, URI or
    collection — only the fusion rule."""
    _wire(monkeypatch, _FakeConn("a", existing=None, physical_taken=True))
    _forbid_ddl(monkeypatch, "no DDL")

    with pytest.raises(ConflictError) as ei:
        await table_service.create_table(
            uuid.uuid4(), "b__c", _COLS, actor_id="t", if_not_exists=True)

    msg = str(ei.value)
    for leaked in ("headline", "created_by", "akb://", "collection_id"):
        assert leaked not in msg


# ── 5. SECURITY: write authority does not confer read authority ──


async def test_no_op_without_read_authority_returns_minimal_envelope(monkeypatch):
    """`token_has_scope` is `"admin" in granted or required in granted` —
    there is no write->read implication. `akb_create_table` is write-scoped
    and `akb_browse` is read-scoped, so a write-only credential can create
    tables and cannot browse. The enriched no-op body (URI, collection,
    columns, keys, indexes) would therefore be a NEW disclosure to it.

    `matches_request` / `mismatches` are withheld too: they are schema
    oracles — repeated probing reconstructs the stored schema without ever
    returning it.
    """
    _wire(monkeypatch, _FakeConn("v", existing=_stored_row()))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t",
        if_not_exists=True, can_read_existing=False)

    assert out == {
        "kind": "table",
        "name": "issues",
        "created": False,
        "outcome": "already_exists",
    }


async def test_read_authority_defaults_closed(monkeypatch):
    """A caller that never passes the capability must not be handed the
    stored state by omission."""
    _wire(monkeypatch, _FakeConn("v", existing=_stored_row()))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t", if_not_exists=True)

    assert "columns" not in out
    assert "mismatches" not in out


async def test_read_authority_grants_the_full_envelope(monkeypatch):
    _wire(monkeypatch, _FakeConn("v", existing=_stored_row()))
    _forbid_ddl(monkeypatch, "no-op")

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t",
        if_not_exists=True, can_read_existing=True)

    assert out["columns"] == _COLS
    assert out["matches_request"] is True
    assert "uri" in out and "collection" in out


async def test_real_create_is_unaffected_by_the_capability(monkeypatch):
    """The capability gates only the no-op projection. A caller that
    actually created the table already knows its schema — it supplied it."""
    conn = _FakeConn("v", existing=None)
    _wire(monkeypatch, conn)

    async def _ok(*a, **k):
        return None

    for fn in ("create_dynamic_table",):
        monkeypatch.setattr(table_service.table_data_repo, fn, _ok)
    monkeypatch.setattr(table_service.table_registry_repo, "insert", _ok)
    monkeypatch.setattr(table_service, "emit_event", _ok)

    async def _no_index(*a, **k):
        return None

    monkeypatch.setattr(table_service, "index_table_metadata", _no_index)

    class _RS:
        async def grant_table_in_conn(self, *a, **k):
            return None

    monkeypatch.setattr(table_service, "get_role_sync", lambda: _RS())

    out = await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t",
        if_not_exists=True, can_read_existing=False)

    assert out["created"] is True
    assert out["columns"] == _COLS


# ── 6. the advisory lock ─────────────────────────────────────────


async def test_advisory_lock_is_taken_before_the_existence_check(monkeypatch):
    """Serialising on `(vault_id, name)` is what removes the create/create
    race, instead of classifying each race outcome after the
    fact — a reread inside an aborted transaction is not implementable."""
    conn = _FakeConn("v", existing=_stored_row())
    _wire(monkeypatch, conn)
    _forbid_ddl(monkeypatch, "no-op")

    await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t", if_not_exists=True)

    assert "advisory_lock" in conn.calls, f"no lock taken; calls={conn.calls}"
    assert conn.calls.index("advisory_lock") < conn.calls.index("find_by_name")


async def test_transaction_pins_read_committed(monkeypatch):
    """The lock only helps if the loser SEES the winner after the wait.
    Under REPEATABLE READ the vault lookup fixes a snapshot before the wait,
    so the loser would still miss the winner. The pool pins no isolation
    level, so a deployment default must not be able to break this."""
    conn = _FakeConn("v", existing=_stored_row())
    _wire(monkeypatch, conn)
    _forbid_ddl(monkeypatch, "no-op")

    await table_service.create_table(
        uuid.uuid4(), "issues", _COLS, actor_id="t",
        if_not_exists=True, can_read_existing=True)

    assert conn.tx_kwargs.get("isolation") == "read_committed"


async def test_lock_key_is_domain_prefixed(monkeypatch):
    """`table_migration_service` calls the same function with an
    UNPREFIXED f"{vault_id}:{key}", so a migration key equal to a table
    name would alias onto this exact lock."""
    captured: list = []

    class _Recorder(_FakeConn):
        async def fetchval(self, sql, *params):
            if "pg_advisory" in sql:
                captured.append(params[0])
            return await super().fetchval(sql, *params)

    conn = _Recorder("v", existing=_stored_row())
    _wire(monkeypatch, conn)
    _forbid_ddl(monkeypatch, "no-op")

    vid = uuid.uuid4()
    await table_service.create_table(
        vid, "issues", _COLS, actor_id="t",
        if_not_exists=True, can_read_existing=True)

    assert captured == [f"table-create:{vid}:issues"]


async def test_advisory_lock_is_taken_on_the_strict_path_too(monkeypatch):
    """The race exists regardless of the flag, so the lock is not
    conditional on it."""
    conn = _FakeConn("v", existing=_stored_row())
    _wire(monkeypatch, conn)
    _forbid_ddl(monkeypatch, "no DDL on a duplicate")

    with pytest.raises(ConflictError):
        await table_service.create_table(
            uuid.uuid4(), "issues", _COLS, actor_id="t")

    assert "advisory_lock" in conn.calls
