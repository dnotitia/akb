"""Archive scope on the VAULT path (akb#530).

The vault path exists so an ordinary search filters by vault id instead of
enumerating candidate source ids. Archive scope used to disqualify it: the
scope defaults to `unarchived` and the gate demanded `all`, so the fast path
had no reachable caller and every default request fell back to enumeration —
which refuses with the bounded-corpus error on a large scope.

The predicate moved to hydration, where the AUTHORITATIVE status is already in
hand. These tests pin both halves: that the exclusion is exact, and that a
page shortened by it is refilled from the prefetch pool rather than returned
short.

They also pin what the exclusion is NOT (akb#604). The drop is counted, but a
filter honouring the request is not a failure, so it never sets `degraded` —
while the five genuine hydration causes, and any retrieval leg that raised,
still do.

And what it IS (akb#608): the count reaches the caller as `excluded`, keyed by
a public cause name. Removing the filter from the failure flag left nothing in
the response to explain a short page; this field is the replacement, and the
assertions below hold the split open from both sides — a fault never appears
in `excluded`, a filter never appears in `degradation_reason`, and a response
carrying both reports each exactly once.

The last section applies the same reconciliation to the FAULTS (akb#611). The
refill loop compensates for them too, and a flag set from the presence of a
fault said a complete page was incomplete. So the fault causes now split by
what they cost: a drop the refill replaced leaves the page whole and is counted
in `recovered`, a drop that left the page short of `limit` still degrades, and
a short page nothing went wrong in stays unflagged. Three fields, three
outcomes, and no drop reported twice.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.services import search_service as ss
from app.services.search_service import SearchService
from app.services.vector_store import VectorHit

pytestmark = pytest.mark.asyncio

VAULT_ID = uuid.UUID(int=7)


def _doc_row(doc_id: uuid.UUID, status: str) -> dict:
    return {
        "id": doc_id,
        "vault_name": "mine",
        "path": f"notes/{doc_id.int}.md",
        "title": f"Doc {doc_id.int}",
        "collection": "notes",
        "doc_type": "note",
        "summary": None,
        "tags": [],
        "status": status,
    }


class _Conn:
    """Answers the vault-path ACL lookup and the legacy-arm hydration join."""

    def __init__(self, statuses: dict[uuid.UUID, str]):
        self.statuses = statuses
        self.queries: list[str] = []

    async def fetchval(self, *_args):
        return False  # is_admin

    async def fetch(self, sql: str, *params):
        self.queries.append(sql)
        if "FROM vaults v WHERE" in sql or "FROM vaults WHERE name" in sql:
            return [{"id": VAULT_ID}]
        if "FROM documents d" in sql:
            wanted = {str(x) for x in params[0]}
            return [
                _doc_row(doc_id, status)
                for doc_id, status in self.statuses.items()
                if str(doc_id) in wanted
            ]
        return []


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _hit(doc_id: uuid.UUID, score: float = 1.0) -> VectorHit:
    return VectorHit(
        chunk_id=str(uuid.uuid4()),
        source_type="document",
        source_id=str(doc_id),
        section_path="",
        content="body",
        score=score,
    )


def _install(monkeypatch, conn):
    from app.config import settings
    from app.services import vault_backfill

    class _Capable:
        vault_filter_supported = True

    async def get_test_pool():
        return _Pool(conn)

    monkeypatch.setattr(settings, "vault_filter_enabled", True, raising=False)
    monkeypatch.setattr(settings, "rerank_enabled", False, raising=False)
    monkeypatch.setattr(ss, "get_vector_store", lambda: _Capable())
    monkeypatch.setattr(vault_backfill, "is_ready", lambda: True)
    monkeypatch.setattr(ss, "get_pool", get_test_pool)
    monkeypatch.setattr(ss, "generate_embeddings", AsyncMock(return_value=[[0.1]]))
    monkeypatch.setattr(ss, "_configured_document_source_type", lambda: "document")


@pytest.mark.parametrize("scope,kept", [
    ("unarchived", False),
    ("all", True),
])
async def test_hydration_applies_archive_scope_to_the_authoritative_status(monkeypatch, scope, kept):
    """`unarchived` excludes an archived document and says so by cause;
    `all` keeps it. The status read is the arm's authority — `documents.status`
    here, verified frontmatter on the native arm."""
    doc_id = uuid.uuid4()
    conn = _Conn({doc_id: "archived"})
    _install(monkeypatch, conn)

    results, dropped = await SearchService()._hydrate_hits([_hit(doc_id)], archive_scope=scope)

    assert len(results) == (1 if kept else 0)
    assert dropped == ({} if kept else {"archive_scope_excluded": 1})


async def test_hydration_defaults_to_no_archive_filtering(monkeypatch):
    """The default keeps every caller that does not pass a scope unchanged —
    the id path already applied the predicate during candidate selection."""
    doc_id = uuid.uuid4()
    conn = _Conn({doc_id: "archived"})
    _install(monkeypatch, conn)

    results, dropped = await SearchService()._hydrate_hits([_hit(doc_id)])

    assert len(results) == 1
    assert dropped == {}


async def test_a_page_shortened_by_the_archive_filter_is_refilled(monkeypatch):
    """An archived hit inside the page must not cost a result slot: the search
    refills from the rest of the deduped prefetch pool. Without the refill this
    returns 2 of the 3 requested.

    The refilled page is COMPLETE, so it is not degraded (akb#604). It used to
    be: the drop counter promoted straight to `degradation_reason`, so a caller
    who got every result they asked for was told the retrieval service had
    failed — and two of the three consumers of that flag discard the results
    before reading them."""
    ids = [uuid.uuid4() for _ in range(6)]
    # The second hit of the page is archived; the pool has spare candidates.
    statuses = {doc_id: ("archived" if i == 1 else "draft") for i, doc_id in enumerate(ids)}
    conn = _Conn(statuses)
    _install(monkeypatch, conn)
    # search_prefetch gives the pool headroom beyond `limit`; with none there
    # is nothing to refill from and the page is legitimately short.
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 6, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=3)

    assert response.returned == 3
    assert [r.status for r in response.results] == ["draft", "draft", "draft"]
    # Pool order is preserved and the archived hit at index 1 is replaced by
    # the next spare candidate (index 3), not by shortening the page.
    assert [r.title for r in response.results] == [
        f"Doc {ids[0].int}", f"Doc {ids[2].int}", f"Doc {ids[3].int}",
    ]
    assert response.degraded is False
    assert response.degradation_reason is None
    # The refill hid the exclusion from the page, not from the response
    # (akb#608): `excluded` counts what the filter removed while assembling
    # this page, so it is non-empty even though the page is full. A caller
    # comparing it against `returned` learns the scope was doing work, which
    # is why it is named for the exclusion and not for the shortfall.
    assert response.excluded == {"archived": 1}


async def test_an_exhausted_pool_returns_a_short_page_without_calling_it_a_failure(monkeypatch):
    """With no spare candidates the page really is short — and that is still
    visible, as the gap between `total_matches` and `returned`.

    What left is the failure flag (akb#604). The page is short because the
    caller asked for `unarchived` and one candidate was archived; a filter
    honouring the request is part of the query, not a fault, so the response
    is not degraded and `degradation_reason` is None. The count carries the
    gap, the way `hits.total` does in Elasticsearch, instead of a signal whose
    every consumer reads it as "the retrieval service is unavailable"."""
    ids = [uuid.uuid4() for _ in range(2)]
    conn = _Conn({ids[0]: "draft", ids[1]: "archived"})
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 0, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=2)

    assert response.returned == 1
    # The short page stays observable: the pool held 2, the caller got 1.
    assert response.total_matches == 2
    assert response.total_matches > response.returned
    assert response.degraded is False
    assert response.degradation_reason is None
    # …and now it is also explicable (akb#608). The gap above says a hit was
    # lost somewhere between the pool and the page; this says which filter took
    # it, which is what separates "your scope removed it" from "something went
    # wrong". The key is the public name — the internal counter string must not
    # reach the response, so that renaming it is not an API break.
    assert response.excluded == {"archived": 1}
    assert "archive_scope_excluded" not in response.excluded
    # Here — pool exhausted, nothing else dropped — the arithmetic closes, so
    # every candidate the caller was told about is accounted for. That is the
    # exhausted-pool case, not a general invariant: a pool with spare left
    # keeps candidates the page never needed.
    assert response.returned + sum(response.excluded.values()) == response.total_matches


async def test_a_genuine_cause_alongside_an_archive_exclusion_still_degrades(monkeypatch):
    """Dropping `archive_scope_excluded` from the flag must not take the other
    five causes with it. Here one candidate is archived (a filter) and one has
    no source row at all (a fault — deleted or stale between retrieval and
    hydration). The response IS degraded, and the reason names only the fault:
    the archive count must not appear in a string the UI renders as a failure."""
    ids = [uuid.uuid4() for _ in range(3)]
    # ids[2] is deliberately absent from the join — the `hydration_miss` shape.
    conn = _Conn({ids[0]: "draft", ids[1]: "archived"})
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 0, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=3)

    assert response.returned == 1
    assert response.degraded is True
    assert response.degradation_reason == "hydration_dropped:hydration_miss=1"
    assert "archive_scope_excluded" not in response.degradation_reason
    # The response carries both causes, and each is reported in exactly one
    # place (akb#608). The fault is in the reason and nowhere else; the filter
    # is in `excluded` and nowhere else. Counting them together would put the
    # same short page under two explanations, one of which says retrying helps.
    assert response.excluded == {"archived": 1}
    assert "hydration_miss" not in response.excluded


async def test_a_failed_retrieval_leg_still_outranks_the_hydration_causes(monkeypatch):
    """Precedence is unchanged: a leg that raised is named in preference to
    anything hydration counted, and an archive exclusion underneath it neither
    adds to nor replaces that reason."""
    ids = [uuid.uuid4() for _ in range(2)]
    conn = _Conn({ids[0]: "draft", ids[1]: "archived"})
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 0, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=(
            [_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)],
            "sparse_encoder_degraded",
        )),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=2)

    assert response.degraded is True
    assert response.degradation_reason == "sparse_encoder_degraded"
    # A leg that raised outranks the hydration causes in the REASON only. The
    # filter still removed a candidate from this page, so it is still counted
    # — the precedence rule picks one string, not one fact.
    assert response.excluded == {"archived": 1}


async def test_a_clean_search_reports_an_empty_exclusion_map(monkeypatch):
    """Nothing filtered, nothing dropped: `excluded` is `{}` and still present
    (akb#608). The field is unconditional so a caller tests it for emptiness
    rather than for existence — `if not response.excluded` is the whole check,
    with no absent/zero distinction to get wrong."""
    ids = [uuid.uuid4() for _ in range(2)]
    conn = _Conn({ids[0]: "draft", ids[1]: "published"})
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 0, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=2)

    assert response.returned == 2
    assert response.excluded == {}
    assert "excluded" in response.model_dump()


async def test_a_fault_alone_leaves_the_exclusion_map_empty(monkeypatch):
    """The five genuine causes belong to `degradation_reason`, so a page short
    for one of them reports nothing here (akb#608). `excluded` promises the
    exclusions that are NOT faults; a non-empty map that could mean either
    would be no better than the flag it replaces."""
    ids = [uuid.uuid4() for _ in range(2)]
    # ids[1] is absent from the join — the `hydration_miss` shape, a fault.
    conn = _Conn({ids[0]: "draft"})
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 0, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=2)

    assert response.returned == 1
    assert response.degraded is True
    assert response.degradation_reason == "hydration_dropped:hydration_miss=1"
    assert response.excluded == {}


async def test_the_public_exclusion_names_are_a_translation_not_a_passthrough():
    """`excluded` is keyed by API vocabulary, `dropped` by internal diagnostic
    strings, and the mapping between them is explicit (akb#608). Two things it
    buys: an internal rename cannot break a caller, and a fault cause cannot
    reach the response by having no entry — it is filtered, not passed through
    unnamed.

    The non-degrading set is derived from the same table, so a cause can never
    be excused from the failure flag without also being given a word the caller
    sees; that is the invariant that keeps a short page explicable."""
    assert ss.PUBLIC_DROP_CAUSE_NAMES["archive_scope_excluded"] == "archived"
    assert set(ss.PUBLIC_DROP_CAUSE_NAMES) == set(ss.NON_DEGRADING_DROP_CAUSES)

    mixed = {"archive_scope_excluded": 2, "hydration_miss": 1, "stale_arm": 3}
    assert ss._public_exclusions(mixed) == {"archived": 2}
    assert ss._public_exclusions({}) == {}


async def test_the_fault_drops_are_the_exact_complement_of_the_exclusions():
    """Both fault fields are built from one classification (akb#611), so a
    cause is either named publicly in `excluded` or carried as a fault, never
    neither and never both. Deriving the complement from the same table — not
    from a second list of fault names — is what makes that hold by
    construction rather than by review."""
    mixed = {"archive_scope_excluded": 2, "hydration_miss": 1, "stale_arm": 3}

    assert ss._fault_drops(mixed) == {"hydration_miss": 1, "stale_arm": 3}
    assert ss._fault_drops({}) == {}
    # Partition: every cause lands in exactly one side, and the counts are
    # preserved whole — neither view invents, merges, or loses a drop.
    assert set(ss._fault_drops(mixed)) | set(ss.PUBLIC_DROP_CAUSE_NAMES) >= set(mixed)
    assert not set(ss._fault_drops(mixed)) & set(ss.PUBLIC_DROP_CAUSE_NAMES)
    assert sum(ss._fault_drops(mixed).values()) + sum(
        ss._public_exclusions(mixed).values()
    ) == sum(mixed.values())


async def test_a_fault_the_refill_replaced_leaves_a_complete_page_unflagged(monkeypatch):
    """The first half of akb#611. A hydration fault inside the page is replaced
    from the spare pool exactly as a filtered hit is, so the caller receives
    every result they asked for — and a response that gave up nothing must not
    claim the result set is incomplete.

    This is the observed shape, not a contrived one: `hydration_miss` means a
    hit survived retrieval and its source row was gone by hydration, which is
    what an ordinary delete looks like from the search side. The flag was set
    from the presence of a fault and never reconciled against what came back,
    so a write race produced eleven consecutive complete-but-degraded pages
    where a component failure produced none."""
    ids = [uuid.uuid4() for _ in range(6)]
    # The second hit of the page has no source row at all — the `hydration_miss`
    # shape, a genuine fault. The pool has spare candidates behind it.
    statuses = {doc_id: "draft" for i, doc_id in enumerate(ids) if i != 1}
    conn = _Conn(statuses)
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 6, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=3)

    # A full page, refilled around the missing row the same way akb#530 refills
    # around a filtered one.
    assert response.returned == 3
    assert [r.title for r in response.results] == [
        f"Doc {ids[0].int}", f"Doc {ids[2].int}", f"Doc {ids[3].int}",
    ]
    assert response.degraded is False
    assert response.degradation_reason is None
    # …and the second half: the fault is not thrown away with the flag. A chunk
    # that pointed at a row that is gone is a corpus-integrity signal worth
    # chasing even though this response lost nothing, so it is reported as a
    # count. Keyed by the internal cause name, which is the same word
    # `degradation_reason` uses for it when it DOES cost something — one fault,
    # one name, whichever field carries it.
    assert response.recovered == {"hydration_miss": 1}
    # A fault is still not an exclusion: `excluded` promises the drops the
    # REQUEST caused, and nothing in this request removed anything.
    assert response.excluded == {}


async def test_a_fault_that_left_the_page_short_still_degrades(monkeypatch):
    """The other half of akb#611, and the case the flag exists for. With the
    pool exhausted the refill has nothing left to substitute, so the caller
    receives fewer results than they asked for BECAUSE of the fault — the
    response really is incomplete and says so.

    `recovered` stays empty here. It counts faults that cost nothing, and this
    one cost a result; reporting it in both places would put the same drop
    under two explanations, one of which says retrying helps and one of which
    says nothing is wrong."""
    ids = [uuid.uuid4() for _ in range(3)]
    # ids[2] is absent from the join — a fault — and there is no spare pool.
    conn = _Conn({ids[0]: "draft", ids[1]: "draft"})
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 0, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=3)

    assert response.returned == 2
    assert response.returned < 3
    assert response.degraded is True
    assert response.degradation_reason == "hydration_dropped:hydration_miss=1"
    assert response.recovered == {}


async def test_a_page_short_for_no_fault_at_all_is_not_degraded(monkeypatch):
    """Why the predicate is a conjunction and not just the page length. A
    corpus holding fewer matches than `limit` returns a short page with nothing
    wrong with it, and reading shortness alone as incompleteness would flag
    every narrow query — the same over-reach as akb#604, from the other
    direction. The fault has to have cost something."""
    ids = [uuid.uuid4() for _ in range(2)]
    conn = _Conn({ids[0]: "draft", ids[1]: "published"})
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 0, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=5)

    assert response.returned == 2
    assert response.returned < 5
    assert response.degraded is False
    assert response.degradation_reason is None
    assert response.recovered == {}
    assert response.excluded == {}


async def test_a_partly_compensated_fault_is_reported_once_as_the_degradation(monkeypatch):
    """The page is the unit, not the candidate. Here the refill replaced one
    dropped hit and then ran out, so some of the fault was paid for and some of
    it was not — and the response is short, which is the fact that matters.

    Splitting the count across both fields would be arithmetic no caller asked
    for and would leave `degraded` and `recovered` disagreeing about the same
    request. `degradation_reason` carries the whole count; `recovered` is empty
    because the page did not recover."""
    ids = [uuid.uuid4() for _ in range(5)]
    # Only ids[1] and ids[2] have source rows; the other three are faults.
    conn = _Conn({ids[1]: "draft", ids[2]: "draft"})
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 5, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=3)

    assert response.returned == 2
    assert response.degraded is True
    assert response.degradation_reason == "hydration_dropped:hydration_miss=3"
    assert response.recovered == {}


async def test_a_complete_page_separates_a_recovered_fault_from_an_excluded_filter(monkeypatch):
    """Both kinds of drop in one page, both compensated, each reported once and
    in its own field. This is the three-way split the series arrived at: the
    request took one candidate out (`excluded`), a fault took another and the
    refill replaced it (`recovered`), and the response is complete so nothing
    is degraded."""
    ids = [uuid.uuid4() for _ in range(6)]
    # ids[1] archived (a filter), ids[2] absent from the join (a fault).
    statuses = {
        doc_id: ("archived" if i == 1 else "draft")
        for i, doc_id in enumerate(ids) if i != 2
    }
    conn = _Conn(statuses)
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 6, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=3)

    assert response.returned == 3
    assert response.degraded is False
    assert response.degradation_reason is None
    assert response.excluded == {"archived": 1}
    assert response.recovered == {"hydration_miss": 1}
    # Neither field borrows the other's vocabulary, in either direction.
    assert "hydration_miss" not in response.excluded
    assert "archived" not in response.recovered
    assert "archive_scope_excluded" not in response.recovered


async def test_a_clean_search_reports_an_empty_recovered_map(monkeypatch):
    """`recovered` is unconditional for the same reason `excluded` is: a caller
    tests it for emptiness, with no absent/zero distinction to get wrong."""
    ids = [uuid.uuid4() for _ in range(2)]
    conn = _Conn({ids[0]: "draft", ids[1]: "published"})
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 0, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=2)

    assert response.returned == 2
    assert response.recovered == {}
    assert "recovered" in response.model_dump()


async def test_a_real_search_records_exactly_one_observation(monkeypatch):
    """The accounting chokepoint, end to end (akb#612). The counter lives here
    rather than in its own module's tests because this is where a real search
    can be driven: the unit tests beside it can prove the arithmetic, but only
    a response that came out of `SearchService.search` proves the wrapper is on
    the path a caller actually takes.

    One search, one observation — not zero (the decorator was dropped) and not
    two (the wrapper was applied twice, or a retry was counted as a request).
    The degraded one is attributed to its cause, which is the parse the section
    depends on."""
    from app.services import search_degradation_stats as sds

    ids = [uuid.uuid4() for _ in range(2)]
    # ids[1] is absent from the join — a fault — and there is no spare pool, so
    # the page comes back short and the response really is degraded.
    conn = _Conn({ids[0]: "draft"})
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 0, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    sds.reset()
    try:
        response = await service.search(
            "x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=2,
        )
        assert response.degraded is True
        assert sds.pending_depth() == 1
        delta = next(iter(sds._pending.values()))
        assert delta.observed == 1
        assert delta.degraded == 1
        # Short page, one result kept — the `degraded_with_results` shape.
        assert delta.degraded_with_results == 1
        assert dict(delta.by_cause) == {"hydration_miss": 1}
    finally:
        sds.reset()


async def test_tables_and_files_carry_no_status_and_survive_the_default_scope(monkeypatch):
    """Only documents have an archived state. `status_matches(None, …)` keeps
    table/file rows under `unarchived` and `all`, matching what the candidate
    branches do with their `scope != "archived"` guards."""
    from app.services.search_filters import status_matches

    assert status_matches(None, "unarchived") is True
    assert status_matches(None, "all") is True
    assert status_matches(None, "archived") is False
