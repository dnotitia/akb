"""Unit tests for what `SearchService.grep(replace=...)` writes — issues #338, #315.

Three defects lived in the same handful of lines, all of the form "the
rewrite touches the wrong thing":

  * **#338 — too many documents.** The `collection` filter compiled to
    `d.path LIKE $n || '%'`: unescaped, so a `_` or `%` in an ordinary
    collection name acted as a wildcard, and unanchored, so `core` also
    swept in the sibling collection `core-extra/`. Every one of those
    documents got a git commit and a re-index.
  * **#315 — too few documents.** The replace loop iterated
    `matched_docs[:limit]` with `limit` clamped to 50, so a wider scope was
    silently rewritten in part and reported as done.
  * **wrong bytes.** With `regex=false` the two case branches disagreed:
    `case_sensitive=True` used `str.replace` (literal) while the default
    `case_sensitive=False` passed the string to `_re.sub`, making it a
    regex *template* — so `TODO\\1` raised mid-loop, `[\\g<0>]` expanded,
    and `C:\\new` wrote a newline.

The DB is mocked, but not vacuously: `_FakeConn` evaluates the collection
predicate the query actually emits, including `ESCAPE` semantics, so the
"prefix-adjacent sibling is untouched" assertion #338 asks for is made
end-to-end against `doc_service.update` call records rather than by
eyeballing the SQL string.
"""
from __future__ import annotations

import re
import uuid

import pytest

from app.services import search_service
from app.services.search_service import SearchService, collection_containment_sql

# asyncio_mode = "auto" (pyproject) marks the async tests; the sync
# predicate tests below must NOT carry an asyncio mark.


# ── a faithful-enough LIKE ────────────────────────────────────────────
# Translating `LIKE ... ESCAPE '\'` to a regex is the only way the fake
# conn can filter the corpus the way PostgreSQL would. Verified against
# PostgreSQL 16 for the cases exercised below.
def _like_match(value: str, pattern: str) -> bool:
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append({"%": ".*", "_": "."}.get(ch, re.escape(ch)))
        i += 1
    return re.fullmatch("".join(out), value, re.DOTALL) is not None


class _FakeConn:
    """asyncpg-conn stand-in for `grep`'s single chunk query.

    Applies the two clauses under test — the content match and the
    collection containment — against `corpus`. The ACL clause is bound but
    not evaluated; it is orthogonal to what these tests pin.
    """

    def __init__(self, corpus: list[dict]):
        self.corpus = corpus
        self.sql: str | None = None
        self.params: tuple = ()

    async def fetch(self, sql: str, *params):
        self.sql, self.params = sql, params
        pos = 0
        pattern = params[pos]; pos += 1
        if "v.name = ANY(" in sql:
            pos += 1
        if "vault_access" in sql:
            pos += 1
        coll_exact = coll_like = None
        if "ESCAPE" in sql:
            coll_exact, coll_like = params[pos], params[pos + 1]

        # `c.content ~`/`~*` in regex mode, `LIKE`/`ILIKE` otherwise.
        is_regex = "c.content ~" in sql
        ci = "ILIKE" in sql or "~*" in sql

        rows = []
        for row in self.corpus:
            if is_regex:
                if not re.search(pattern, row["content"], re.I if ci else 0):
                    continue
            elif ci:
                if pattern.lower() not in row["content"].lower():
                    continue
            elif pattern not in row["content"]:
                continue
            if coll_like is not None and not (
                row["path"] == coll_exact or _like_match(row["path"], coll_like)
            ):
                continue
            rows.append(row)
        return rows


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _CM()


class _Doc:
    def __init__(self, content):
        self.content = content


class _PutResult:
    def __init__(self, commit_hash, previous_commit):
        self.commit_hash = commit_hash
        self.previous_commit = previous_commit


class _FakeDocService:
    """Records every write. `fail_on` makes the Nth update raise."""

    def __init__(self, bodies: dict[str, str], fail_on: int | None = None):
        self.bodies = bodies
        self.fail_on = fail_on
        self.updated: list[tuple[str, str, str]] = []  # (vault, path, new_body)

    async def get(self, vault, path):
        return _Doc(self.bodies[path])

    async def update(self, vault, path, req, agent_id=None):
        if self.fail_on is not None and len(self.updated) == self.fail_on:
            raise RuntimeError("write lane busy")
        self.updated.append((vault, path, req.content))
        n = len(self.updated)
        return _PutResult(f"commit{n}", f"parent{n}")


def _chunk(path: str, content: str, *, vault: str = "v", title: str = "T") -> dict:
    return {
        "doc_id": str(uuid.uuid5(uuid.NAMESPACE_URL, path)),
        "vault": vault,
        "path": path,
        "title": title,
        "metadata": None,
        "section_path": "# S",
        "content": content,
        "chunk_index": 0,
    }


def _wire(monkeypatch, corpus, doc_service=None):
    conn = _FakeConn(corpus)
    monkeypatch.setattr(search_service, "get_pool", lambda: _async(_FakePool(conn)))
    return conn, doc_service


async def _async(v):
    return v


# ── #338: containment predicate ───────────────────────────────────────

def test_containment_sql_is_anchored_and_escaped():
    params: list = []
    sql = collection_containment_sql("d.path", "core", params)
    assert sql == "(d.path = $1 OR d.path LIKE $2 ESCAPE '\\')"
    assert params == ["core", "core/%"]


def test_containment_sql_escapes_like_metacharacters_in_a_real_name():
    params: list = []
    collection_containment_sql("d.path", "my_docs", params)
    # The `_` must reach PostgreSQL escaped, or it matches any character.
    assert params == ["my_docs", "my\\_docs/%"]
    assert _like_match("my_docs/a.md", params[1])
    assert not _like_match("myXdocs/a.md", params[1])


def test_containment_sql_neutralises_a_wildcard_collection():
    params: list = []
    collection_containment_sql("d.path", "%", params)
    assert params == ["%", "\\%/%"]
    assert not _like_match("secret/x.md", params[1])


def test_containment_sql_tolerates_surrounding_slashes():
    params: list = []
    collection_containment_sql("d.path", "/core/", params)
    assert params == ["core", "core/%"]


def test_containment_admits_descendants_and_excludes_prefix_siblings():
    params: list = []
    collection_containment_sql("d.path", "core", params)
    like = params[1]
    assert _like_match("core/a.md", like)
    assert _like_match("core/deep/b.md", like)          # nested, still inside
    assert not _like_match("core-extra/b.md", like)     # the #338 leak
    assert not _like_match("coreutils.md", like)


async def test_replace_scoped_to_a_collection_leaves_the_sibling_untouched(monkeypatch):
    """The regression #338 asks for: a rewrite scoped to `core` must not
    commit anything in `core-extra`."""
    corpus = [
        _chunk("core/a.md", "needle here"),
        _chunk("core/deep/b.md", "needle nested"),
        _chunk("core-extra/prefix-adjacent.md", "needle adjacent"),
    ]
    docs = _FakeDocService({
        "core/a.md": "needle here",
        "core/deep/b.md": "needle nested",
        "core-extra/prefix-adjacent.md": "needle adjacent",
    })
    _wire(monkeypatch, corpus)

    resp = await SearchService().grep(
        "needle", vault="v", collection="core", replace="thread",
        doc_service=docs, agent_id="a", user_id=str(uuid.uuid4()),
    )

    touched = {p for _, p, _ in docs.updated}
    assert touched == {"core/a.md", "core/deep/b.md"}
    assert "core-extra/prefix-adjacent.md" not in touched
    assert resp["replaced_docs"] == 2


async def test_every_placeholder_is_bound_with_all_filters_on(monkeypatch):
    """The containment predicate binds TWO params where the old one bound one.

    Every `$n` downstream of it shifts, and a fake connection will happily
    accept a query whose placeholders no longer line up with its params —
    only PostgreSQL would reject that. Pin the invariant here instead.
    """
    conn = _FakeConn([_chunk("core/a.md", "needle")])
    monkeypatch.setattr(search_service, "get_pool", lambda: _async(_FakePool(conn)))

    await SearchService().grep(
        "needle", vault="v", collection="core", regex=True,
        case_sensitive=True, user_id=str(uuid.uuid4()),
    )

    used = {int(n) for n in re.findall(r"\$(\d+)", conn.sql)}
    assert used == set(range(1, len(conn.params) + 1)), (
        f"placeholders {sorted(used)} vs {len(conn.params)} bound params"
    )
    # ...and the containment pair is the last thing bound, in order.
    assert conn.params[-2:] == ("core", "core/%")


async def test_replace_scoped_to_an_underscore_collection_is_not_a_wildcard(monkeypatch):
    corpus = [_chunk("my_docs/a.md", "needle"), _chunk("myXdocs/a.md", "needle")]
    docs = _FakeDocService({"my_docs/a.md": "needle", "myXdocs/a.md": "needle"})
    _wire(monkeypatch, corpus)

    await SearchService().grep(
        "needle", vault="v", collection="my_docs", replace="thread",
        doc_service=docs, agent_id="a", user_id=str(uuid.uuid4()),
    )
    assert [p for _, p, _ in docs.updated] == ["my_docs/a.md"]


# ── #315: replace covers the whole matched set ────────────────────────

async def test_replace_covers_every_match_not_just_the_limit_page(monkeypatch):
    n = 60  # > search_limit_max (50), so the old slice could never reach these
    corpus = [_chunk(f"c/{i:03}.md", "needle") for i in range(n)]
    docs = _FakeDocService({f"c/{i:03}.md": "needle" for i in range(n)})
    _wire(monkeypatch, corpus)

    resp = await SearchService().grep(
        "needle", vault="v", replace="thread", limit=5,
        doc_service=docs, agent_id="a", user_id=str(uuid.uuid4()),
    )

    assert len(docs.updated) == n
    assert resp["replaced_docs"] == n
    # `limit` still governs the preview, and only the preview.
    assert resp["returned_docs"] == 5
    assert resp["total_docs"] == n
    assert resp["truncated"] is True
    assert "replace covered all 60 matching docs" in resp["hint"]


async def test_replace_over_budget_is_rejected_with_zero_writes(monkeypatch):
    monkeypatch.setattr(search_service.settings, "grep_replace_max_docs", 10)
    corpus = [_chunk(f"c/{i:03}.md", "needle") for i in range(11)]
    docs = _FakeDocService({f"c/{i:03}.md": "needle" for i in range(11)})
    _wire(monkeypatch, corpus)

    resp = await SearchService().grep(
        "needle", vault="v", replace="thread",
        doc_service=docs, agent_id="a", user_id=str(uuid.uuid4()),
    )

    assert docs.updated == []          # fails CLOSED — nothing written
    assert resp["code"] == "replace_scope_too_large"
    assert resp["total_docs"] == 11
    assert resp["max_replacements"] == 10
    assert "replaced_docs" not in resp


async def test_replace_at_exactly_the_budget_is_allowed(monkeypatch):
    monkeypatch.setattr(search_service.settings, "grep_replace_max_docs", 10)
    corpus = [_chunk(f"c/{i:03}.md", "needle") for i in range(10)]
    docs = _FakeDocService({f"c/{i:03}.md": "needle" for i in range(10)})
    _wire(monkeypatch, corpus)

    resp = await SearchService().grep(
        "needle", vault="v", replace="thread",
        doc_service=docs, agent_id="a", user_id=str(uuid.uuid4()),
    )
    assert resp["replaced_docs"] == 10


async def test_empty_replacement_is_still_a_write_and_still_budgeted(monkeypatch):
    """`replace=""` deletes every match — truthiness must not gate the budget."""
    monkeypatch.setattr(search_service.settings, "grep_replace_max_docs", 2)
    corpus = [_chunk(f"c/{i}.md", "needle") for i in range(3)]
    docs = _FakeDocService({f"c/{i}.md": "needle" for i in range(3)})
    _wire(monkeypatch, corpus)

    resp = await SearchService().grep(
        "needle", vault="v", replace="",
        doc_service=docs, agent_id="a", user_id=str(uuid.uuid4()),
    )
    assert docs.updated == []
    assert resp["code"] == "replace_scope_too_large"


# ── the replacement string is data, not a template ────────────────────

@pytest.mark.parametrize("case_sensitive", [True, False])
@pytest.mark.parametrize(
    "replacement",
    [r"C:\new", r"TODO\1", r"[\g<0>]", r"back\\slash", "plain"],
)
async def test_non_regex_replacement_is_literal_on_both_case_branches(
    monkeypatch, case_sensitive, replacement
):
    corpus = [_chunk("c/a.md", "see TODO here")]
    docs = _FakeDocService({"c/a.md": "see TODO here"})
    _wire(monkeypatch, corpus)

    resp = await SearchService().grep(
        "TODO", vault="v", replace=replacement, case_sensitive=case_sensitive,
        doc_service=docs, agent_id="a", user_id=str(uuid.uuid4()),
    )

    assert "error" not in resp
    assert [b for _, _, b in docs.updated] == [f"see {replacement} here"]


async def test_regex_replacement_still_expands_backreferences(monkeypatch):
    corpus = [_chunk("c/a.md", "v1.1 released")]
    docs = _FakeDocService({"c/a.md": "v1.1 released"})
    _wire(monkeypatch, corpus)

    await SearchService().grep(
        r"v(\d+)\.1", vault="v", regex=True, replace=r"v\1.2",
        doc_service=docs, agent_id="a", user_id=str(uuid.uuid4()),
    )
    assert [b for _, _, b in docs.updated] == ["v1.2 released"]


async def test_bad_regex_replacement_template_writes_nothing(monkeypatch):
    corpus = [_chunk(f"c/{i}.md", "TODO") for i in range(3)]
    docs = _FakeDocService({f"c/{i}.md": "TODO" for i in range(3)})
    _wire(monkeypatch, corpus)

    resp = await SearchService().grep(
        "TODO", vault="v", regex=True, replace=r"\1",
        doc_service=docs, agent_id="a", user_id=str(uuid.uuid4()),
    )

    assert docs.updated == []
    assert resp["code"] == "invalid_replacement"
    assert resp["replaced_docs"] == 0


# ── a partial run is recoverable ──────────────────────────────────────

async def test_a_mid_loop_write_failure_reports_what_already_committed(monkeypatch):
    corpus = [_chunk(f"c/{i}.md", "needle") for i in range(5)]
    docs = _FakeDocService(
        {f"c/{i}.md": "needle" for i in range(5)}, fail_on=2
    )
    _wire(monkeypatch, corpus)

    resp = await SearchService().grep(
        "needle", vault="v", replace="thread",
        doc_service=docs, agent_id="a", user_id=str(uuid.uuid4()),
    )

    assert len(docs.updated) == 2
    assert resp["code"] == "replace_incomplete"
    assert resp["replaced_docs"] == 2
    assert resp["remaining_docs"] == 3
    # Both ends of every landed edit, so the caller can unwind or resume.
    assert [r["commit"] for r in resp["replacements"]] == ["commit1", "commit2"]
    assert [r["previous_commit"] for r in resp["replacements"]] == ["parent1", "parent2"]


async def test_an_unreadable_document_is_reported_without_aborting_the_run(monkeypatch):
    """A missing doc is per-document (unlike a bad template), so the rest
    of the scope still gets rewritten."""
    corpus = [_chunk(f"c/{i}.md", "needle") for i in range(3)]
    bodies = {f"c/{i}.md": "needle" for i in range(3)}
    del bodies["c/1.md"]
    docs = _FakeDocService(bodies)
    _wire(monkeypatch, corpus)

    resp = await SearchService().grep(
        "needle", vault="v", replace="thread",
        doc_service=docs, agent_id="a", user_id=str(uuid.uuid4()),
    )

    assert [p for _, p, _ in docs.updated] == ["c/0.md", "c/2.md"]
    assert resp["replaced_docs"] == 3  # 2 commits + 1 recorded failure
    assert [r.get("error") for r in resp["replacements"]] == [None, "not found", None]
