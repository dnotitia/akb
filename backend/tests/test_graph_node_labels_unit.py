"""Graph node and relation labels — DB-free coverage.

A graph node, a vault-overview node and a relation row each name a resource, and
they were all naming a document by its collection path and file name even though
it had a real title. The lookup behind them (``_batch_resolve_labels``) asked the
legacy ``documents`` catalog alone; a document written by the native ledger has
no row there (its title lives in the head revision's frontmatter), so on a
native-authoritative installation every document missed and fell back to its path.

This file pins what needs no database: the label shape, the fallbacks, the
installation-authority switch, the query budget (a constant number of statements
however many nodes — never one per node) and how every projection applies the
label. What the SQL itself does with real native and catalog rows runs against
PostgreSQL in ``test_graph_node_labels_postgres.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from app.services import kg_service
from app.services.document_service import _compose_markdown
from app.services.uri_service import doc_uri, file_uri, table_uri

VAULT = "labels"
OTHER = "elsewhere"
VAULT_ID = uuid.uuid4()
A, B, C = "specs/a.md", "specs/b.md", "notes/c.md"

# The window the first read uses (+1: see `_native_doc_labels`) and the wider one
# a document with a very long frontmatter gets.
FIRST_WINDOW = 8 * 1024 + 1
RETRY_WINDOW = 128 * 1024

# Two moments a store can date a resource by, and what the label serves for them
# (UTC to the second, truncated). T1 carries microseconds on purpose.
T1 = datetime(2026, 10, 20, 13, 8, 25, 987654, tzinfo=UTC)
T1_STAMP = "2026-10-20T13:08:25Z"
T2 = datetime(2026, 10, 21, 2, 0, 0, tzinfo=UTC)
T2_STAMP = "2026-10-21T02:00:00Z"


class _Acquire:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _authority(monkeypatch, *, native: bool) -> None:
    monkeypatch.setattr(
        "app.services.document_counters.native_documents_are_authoritative", lambda: native,
    )


def _body(title: str | None = "T", **fm: Any) -> bytes:
    meta = {} if title is None else {"title": title}
    return _compose_markdown({**meta, **fm}, "body text").encode()


class _LabelConn:
    """Answers the label queries from in-memory catalogs and records them.

    ``legacy`` rows are what the ``documents`` catalog holds; ``native`` maps a
    path to the body bytes of a live native head. The native query joins both
    (the real one is in ``kg_service._NATIVE_DOC_LABELS_SQL``), so the fake does
    too: a retained catalog row is matched by id when a native resource holds the
    path, by path when none does.
    """

    def __init__(self, *, legacy=(), native=None, native_times=None, tables=(), files=()) -> None:
        self.legacy = {row["path"]: row for row in legacy}
        self.native: dict[str, dict] = {
            path: {"resource_id": uuid.uuid4(), "body": body}
            for path, body in (native or {}).items()
        }
        # `native_resources.updated_at` by path; a path left out has no time.
        self.native_times: dict[str, datetime] = dict(native_times or {})
        self.tables = list(tables)
        self.files = list(files)
        self.queries: list[str] = []
        self.native_windows: list[tuple[int, list[str]]] = []

    @property
    def document_queries(self) -> list[str]:
        return [q for q in self.queries if "FROM documents" in q or "native_resources" in q]

    async def fetch(self, query: str, *args):
        self.queries.append(query)
        if "native_resources" in query:
            _vault_id, paths, window = args
            self.native_windows.append((window, list(paths)))
            return [self._native_row(path, window) for path in paths]
        if "FROM documents" in query:
            paths, _vault_id = args
            return [self.legacy[p] for p in paths if p in self.legacy]
        if "FROM vault_tables" in query:
            names, _vault_id = args
            return [row for row in self.tables if row["name"] in names]
        if "FROM vault_files" in query:
            ids, _vault_id = args
            return [row for row in self.files if row["id"] in ids]
        raise AssertionError(f"unexpected query: {query}")

    def _native_row(self, path: str, window: int) -> dict:
        resource = self.native.get(path)
        # A retained catalog row: by id when a native resource holds the path
        # (the fake models the bridge, where the two ids are equal), else by path.
        catalog = self.legacy.get(path)
        body = resource["body"] if resource else None  # None: a head with no payload row
        row = {
            "path": path,
            "resource_id": resource["resource_id"] if resource else None,
            # COALESCE(r.updated_at, dp.updated_at): the resource's time when the
            # ledger holds the path, else the catalog row's (which only joins then).
            "updated_at": (
                self.native_times.get(path) if resource
                else (catalog.get("updated_at") if catalog is not None else None)
            ),
            "body_size": len(body) if body is not None else None,
            "body_slice": body[:window] if body is not None else None,
            "legacy_id": None,
            "legacy_title": None,
            "legacy_doc_type": None,
            "legacy_tags": None,
        }
        if catalog is not None:
            row.update(
                legacy_id=catalog.get("id", uuid.uuid4()),
                legacy_title=catalog["title"],
                legacy_doc_type=catalog.get("doc_type"),
                legacy_tags=catalog.get("tags"),
            )
        return row


def _catalog(path: str, title: str, doc_type: str | None = None, tags=None, updated_at=None) -> dict:
    return {"path": path, "title": title, "doc_type": doc_type, "tags": tags, "updated_at": updated_at}


async def _labels(conn, *uris_and_types: tuple[str, str], vault: str = VAULT) -> dict:
    return await kg_service._batch_resolve_labels(
        conn, list(uris_and_types), vault_id=VAULT_ID, vault_name=vault,
    )


def _doc(path: str, vault: str = VAULT) -> tuple[str, str]:
    return doc_uri(vault, path), "doc"


# ── The label of a document, legacy catalog ──────────────────────────────


@pytest.mark.asyncio
async def test_a_document_is_named_by_its_title_and_described(monkeypatch):
    _authority(monkeypatch, native=False)
    conn = _LabelConn(legacy=[_catalog(A, "Alpha spec", "spec", ["x", "y"])])

    labels = await _labels(conn, _doc(A))

    assert labels == {
        doc_uri(VAULT, A): {
            "name": "Alpha spec",
            "title": "Alpha spec",
            "doc_type": "spec",
            "tags": ["x", "y"],
            "path": A,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["", "   ", None])
async def test_a_document_without_a_title_is_named_by_its_path(monkeypatch, title):
    """Today's value is kept — and no title is invented to stand in for it."""
    _authority(monkeypatch, native=False)
    conn = _LabelConn(legacy=[_catalog(B, title)])

    label = (await _labels(conn, _doc(B)))[doc_uri(VAULT, B)]

    assert label["name"] == B
    assert "title" not in label
    assert (label["doc_type"], label["tags"], label["path"]) == ("note", [], B)


@pytest.mark.asyncio
async def test_a_document_the_vault_does_not_hold_is_named_by_its_path_only(monkeypatch):
    _authority(monkeypatch, native=False)

    labels = await _labels(_LabelConn(), _doc(A))

    assert labels == {doc_uri(VAULT, A): {"name": A, "path": A}}


@pytest.mark.asyncio
async def test_a_uri_from_another_vault_is_ignored_not_echoed(monkeypatch):
    _authority(monkeypatch, native=False)
    conn = _LabelConn(legacy=[_catalog(A, "Alpha spec")])

    labels = await _labels(conn, _doc(A, vault=OTHER), _doc(A))

    assert set(labels) == {doc_uri(VAULT, A)}


@pytest.mark.asyncio
async def test_tables_and_files_keep_their_name_and_repeat_it_as_title(monkeypatch):
    _authority(monkeypatch, native=False)
    file_id = str(uuid.uuid4())
    conn = _LabelConn(
        tables=[
            {"name": "metrics", "description": "Key metrics"},
            {"name": "plain", "description": None},
        ],
        files=[{"id": file_id, "name": "diagram.png"}],
    )

    labels = await _labels(
        conn,
        (table_uri(VAULT, "metrics"), "table"),
        (table_uri(VAULT, "plain"), "table"),
        (table_uri(VAULT, "ghost"), "table"),
        (file_uri(VAULT, file_id), "file"),
        (file_uri(VAULT, "no-such-file"), "file"),
    )

    assert labels[table_uri(VAULT, "metrics")] == {"name": "Key metrics", "title": "Key metrics"}
    assert labels[table_uri(VAULT, "plain")] == {"name": "plain", "title": "plain"}
    assert labels[file_uri(VAULT, file_id)] == {"name": "diagram.png", "title": "diagram.png"}
    # Unresolved: the identifier, and nothing that claims it was found.
    assert labels[table_uri(VAULT, "ghost")] == {"name": "ghost"}
    assert labels[file_uri(VAULT, "no-such-file")] == {"name": "no-such-file"}


@pytest.mark.asyncio
async def test_every_label_has_a_name_that_says_something(monkeypatch):
    _authority(monkeypatch, native=False)
    conn = _LabelConn(
        legacy=[_catalog(A, "")],
        tables=[{"name": "t", "description": ""}],
        files=[{"id": "f1", "name": ""}],
    )

    labels = await _labels(
        conn, _doc(A), (table_uri(VAULT, "t"), "table"), (file_uri(VAULT, "f1"), "file"),
    )

    assert all(label["name"] for label in labels.values())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["a", " ", "b"], ["a", "b"]),
        ("solo", ["solo"]),
        (["a", 2, 3.5], ["a", "2", "3.5"]),
        ([["nested"], {"k": 1}, None], []),
        (None, []),
        (7, []),
    ],
)
def test_tags_are_a_list_of_non_blank_strings(raw, expected):
    assert kg_service._tag_list(raw) == expected


# ── The query budget ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 7, 200])
@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
async def test_labels_cost_one_query_per_resource_type_however_many_nodes(monkeypatch, native, count):
    """The reason the label is batched: a 200-node graph is not 200 lookups."""
    _authority(monkeypatch, native=native)
    paths = [f"bulk/doc-{i}.md" for i in range(count)]
    names = [f"t{i}" for i in range(count)]
    file_ids = [str(uuid.uuid4()) for _ in range(count)]
    conn = _LabelConn(
        legacy=[] if native else [_catalog(p, f"Title {p}") for p in paths],
        native={p: _body(f"Title {p}") for p in paths} if native else None,
        tables=[{"name": n, "description": None} for n in names],
        files=[{"id": fid, "name": fid} for fid in file_ids],
    )

    labels = await _labels(
        conn,
        *[_doc(p) for p in paths],
        *[(table_uri(VAULT, n), "table") for n in names],
        *[(file_uri(VAULT, fid), "file") for fid in file_ids],
    )

    assert len(labels) == 3 * count
    assert len(conn.queries) == 3, conn.queries  # documents, tables, files
    assert len(conn.document_queries) == 1


@pytest.mark.asyncio
async def test_nothing_to_resolve_asks_nothing(monkeypatch):
    _authority(monkeypatch, native=True)
    conn = _LabelConn()

    assert await _labels(conn) == {}
    assert conn.queries == []


# ── The installation authority ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_legacy_installation_never_queries_the_native_ledger(monkeypatch):
    _authority(monkeypatch, native=False)
    conn = _LabelConn(legacy=[_catalog(A, "Alpha spec")])

    await _labels(conn, _doc(A))

    assert conn.queries and not any("native_" in query for query in conn.queries)


@pytest.mark.asyncio
async def test_a_native_document_is_named_by_the_title_in_its_head_frontmatter(monkeypatch):
    """The reported bug: no catalog row exists, and the title is in the body."""
    _authority(monkeypatch, native=True)
    conn = _LabelConn(native={A: _body("Alpha design", type="spec", tags=["graph", "design"])})

    labels = await _labels(conn, _doc(A))

    assert labels == {
        doc_uri(VAULT, A): {
            "name": "Alpha design",
            "title": "Alpha design",
            "doc_type": "spec",
            "tags": ["graph", "design"],
            "path": A,
        },
    }


@pytest.mark.asyncio
async def test_a_native_document_without_a_title_is_named_by_its_path(monkeypatch):
    _authority(monkeypatch, native=True)
    conn = _LabelConn(native={B: _body(None)})

    label = (await _labels(conn, _doc(B)))[doc_uri(VAULT, B)]

    assert label == {"name": B, "doc_type": "note", "tags": [], "path": B}


@pytest.mark.asyncio
async def test_a_plain_markdown_native_body_has_no_frontmatter_to_read(monkeypatch):
    _authority(monkeypatch, native=True)
    conn = _LabelConn(native={B: b"# just a heading\n\nand prose\n"})

    label = (await _labels(conn, _doc(B)))[doc_uri(VAULT, B)]

    assert (label["name"], label["doc_type"]) == (B, "note")


@pytest.mark.asyncio
async def test_a_retained_catalog_row_fills_only_what_the_native_frontmatter_leaves_out(monkeypatch):
    """A document migrated at cutover keeps its row; its preserved body may not
    state the metadata the catalog served. Native states first, the row fills."""
    _authority(monkeypatch, native=True)
    conn = _LabelConn(
        native={A: _body(None, type="decision"), B: _body("Fresh title", tags=["new"])},
        legacy=[_catalog(A, "Catalog title", "note", ["old"]), _catalog(B, "Stale title", "spec", ["old"])],
    )

    labels = await _labels(conn, _doc(A), _doc(B))

    assert labels[doc_uri(VAULT, A)] == {
        "name": "Catalog title", "title": "Catalog title",
        "doc_type": "decision",  # stated by the body, so the row does not override it
        "tags": ["old"],  # not stated by the body, so the row supplies it
        "path": A,
    }
    assert labels[doc_uri(VAULT, B)] == {
        "name": "Fresh title", "title": "Fresh title",  # the body wins over a stale row
        "doc_type": "spec",
        "tags": ["new"],
        "path": B,
    }


@pytest.mark.asyncio
async def test_a_pre_cutover_document_only_the_catalog_holds_still_resolves(monkeypatch):
    _authority(monkeypatch, native=True)
    conn = _LabelConn(legacy=[_catalog(C, "Old note", "note", ["t"])])

    label = (await _labels(conn, _doc(C)))[doc_uri(VAULT, C)]

    assert label == {"name": "Old note", "title": "Old note", "doc_type": "note", "tags": ["t"], "path": C}


@pytest.mark.asyncio
async def test_a_document_in_neither_catalog_is_named_by_its_path_on_a_native_installation(monkeypatch):
    _authority(monkeypatch, native=True)

    labels = await _labels(_LabelConn(), _doc(A))

    assert labels == {doc_uri(VAULT, A): {"name": A, "path": A}}


@pytest.mark.asyncio
async def test_a_native_head_with_no_readable_payload_degrades_to_the_path(monkeypatch):
    """A label is decoration: a damaged head must not take the graph down."""
    _authority(monkeypatch, native=True)
    conn = _LabelConn(native={A: None})  # the resource is there, its payload row is not

    labels = await _labels(conn, _doc(A))

    assert labels == {doc_uri(VAULT, A): {"name": A, "doc_type": "note", "tags": [], "path": A}}


# ── A frontmatter longer than the first read window ──────────────────────


def _wide_body(title: str, *, entries: int) -> bytes:
    """A hub whose relation list pushes `title` (sorted late) past the first window."""
    related = [f"akb://{VAULT}/coll/hub/doc/child-{i:04d}.md" for i in range(entries)]
    return _body(title, related_to=related, type="index")


@pytest.mark.asyncio
async def test_a_wide_frontmatter_gets_one_wider_read_for_those_documents_only(monkeypatch):
    _authority(monkeypatch, native=True)
    wide = _wide_body("Hub index", entries=300)
    assert FIRST_WINDOW < len(wide) < RETRY_WINDOW
    conn = _LabelConn(native={"hub/index.md": wide, A: _body("Alpha")})

    labels = await _labels(conn, _doc("hub/index.md"), _doc(A))

    assert labels[doc_uri(VAULT, "hub/index.md")]["title"] == "Hub index"
    assert labels[doc_uri(VAULT, "hub/index.md")]["doc_type"] == "index"
    assert labels[doc_uri(VAULT, A)]["title"] == "Alpha"
    assert [(window, sorted(paths)) for window, paths in conn.native_windows] == [
        (FIRST_WINDOW, sorted(["hub/index.md", A])),
        (RETRY_WINDOW, ["hub/index.md"]),  # only the one that did not fit
    ]


@pytest.mark.asyncio
async def test_a_short_body_with_a_broken_envelope_is_not_read_again(monkeypatch):
    """Nothing lies beyond the window, so a second read could only repeat it."""
    _authority(monkeypatch, native=True)
    conn = _LabelConn(native={B: b"---\ntitle: never closed\n\nbody\n"})

    label = (await _labels(conn, _doc(B)))[doc_uri(VAULT, B)]

    assert label["name"] == B and "title" not in label
    assert len(conn.native_windows) == 1


@pytest.mark.asyncio
async def test_a_frontmatter_wider_than_the_second_window_falls_back_to_the_path(monkeypatch):
    _authority(monkeypatch, native=True)
    huge = _wide_body("Never seen", entries=6000)
    assert len(huge) > RETRY_WINDOW
    conn = _LabelConn(native={"hub/huge.md": huge})

    label = (await _labels(conn, _doc("hub/huge.md")))[doc_uri(VAULT, "hub/huge.md")]

    assert label["name"] == "hub/huge.md" and "title" not in label
    assert [window for window, _ in conn.native_windows] == [FIRST_WINDOW, RETRY_WINDOW]


@pytest.mark.asyncio
async def test_the_wider_read_is_bounded_in_rows(monkeypatch):
    """A writer must not be able to turn a label into an unbounded read — and the
    documents the bound leaves unread keep what the first read already fetched,
    their time included."""
    _authority(monkeypatch, native=True)
    count = kg_service._LABEL_RETRY_MAX_ROWS + 10
    paths = [f"hub/wide-{i:03d}.md" for i in range(count)]
    # Every document has a moment of its own, so a label that lost its time cannot
    # pass for one that kept a neighbour's. Written out here, not by the code under test.
    moments = {p: T1 + timedelta(seconds=i) for i, p in enumerate(paths)}
    conn = _LabelConn(
        native={p: _wide_body(f"Title {p}", entries=300) for p in paths}, native_times=moments,
    )

    labels = await _labels(conn, *[_doc(p) for p in paths])

    assert len(labels) == count  # every document is still labelled
    assert len(conn.native_windows[1][1]) == kg_service._LABEL_RETRY_MAX_ROWS
    unread = [uri for uri, label in labels.items() if "title" not in label]
    assert len(unread) == 10 and all(labels[uri]["name"] == labels[uri]["path"] for uri in unread)
    # The first statement fetched every document's time whether or not its
    # frontmatter closed inside the window. The ten the cap left unread still state it.
    assert {uri: label.get("updated_at") for uri, label in labels.items()} == {
        doc_uri(VAULT, p): moments[p].strftime("%Y-%m-%dT%H:%M:%SZ") for p in paths
    }
    assert all(labels[doc_uri(VAULT, p)]["path"] == p for p in paths)


@pytest.mark.asyncio
async def test_a_document_the_cap_leaves_unread_keeps_what_the_first_read_knew(monkeypatch):
    """The retained catalog row and the resource's time both come out of the first
    statement. A document that never gets the wider read is named by that row,
    exactly as one whose frontmatter outgrows the wider window is."""
    _authority(monkeypatch, native=True)
    count = kg_service._LABEL_RETRY_MAX_ROWS + 5
    paths = [f"hub/wide-{i:03d}.md" for i in range(count)]
    conn = _LabelConn(
        native={p: _wide_body(f"Body title {p}", entries=300) for p in paths},
        native_times={p: T2 for p in paths},
        legacy=[_catalog(p, f"Catalog title {p}", "decision", ["kept"]) for p in paths],
    )

    labels = await _labels(conn, *[_doc(p) for p in paths])

    read = [label for label in labels.values() if label.get("title", "").startswith("Body title")]
    unread = [label for label in labels.values() if label.get("title", "").startswith("Catalog title")]
    assert (len(read), len(unread)) == (kg_service._LABEL_RETRY_MAX_ROWS, 5)
    for label in read:  # the body states its own type; the row fills the tags it leaves out
        assert (label["doc_type"], label["tags"]) == ("index", ["kept"])
    for label in unread:  # no body was read: the row says everything it knows
        assert (label["doc_type"], label["tags"]) == ("decision", ["kept"])
    assert {label["updated_at"] for label in labels.values()} == {T2_STAMP}
    assert {label["name"] for label in unread} == {label["title"] for label in unread}


# ── When the resource last changed ───────────────────────────────────────
#
# `updated_at` rides in the same statements as the rest of the label. What it
# means on each authority is pinned against real rows in the PostgreSQL file;
# here: the shape of the stamp, which store dates which resource, and that a
# resource with no time states none.


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # UTC to the second, and truncated, never rounded up past the change.
        (T1, T1_STAMP),
        (datetime(2026, 10, 20, 13, 8, 25, 999999, tzinfo=UTC), T1_STAMP),
        (datetime(2026, 10, 20, 13, 8, 25, tzinfo=UTC), T1_STAMP),
        # Another offset is converted, not printed as it came.
        (datetime(2026, 10, 20, 22, 8, 25, 1, tzinfo=timezone(timedelta(hours=9))), T1_STAMP),
        (datetime(2026, 10, 20, 8, 8, 25, tzinfo=timezone(timedelta(hours=-5))), T1_STAMP),
        # A naive value is UTC, whatever the host zone is.
        (datetime(2026, 10, 20, 13, 8, 25, 5), T1_STAMP),
        # No time to give: unknown stays unknown instead of becoming a string.
        (None, None),
        (T1_STAMP, None),
        (1_760_965_705, None),
        # PostgreSQL's `infinity` / `-infinity` come through as the ends of the range
        # and name no moment.
        (datetime.max, None),
        (datetime.min, None),
        (datetime.max.replace(tzinfo=UTC), None),
        (datetime.min.replace(tzinfo=UTC), None),
        # A value whose offset pushes it out of range does not convert; that is
        # "unknown", not an error.
        (datetime(9999, 12, 31, 23, 0, 0, tzinfo=timezone(timedelta(hours=-5))), None),
        (datetime(1, 1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=5))), None),
    ],
)
def test_a_time_is_served_as_utc_to_the_second_or_not_at_all(value, expected):
    assert kg_service._utc_stamp(value) == expected


@pytest.mark.asyncio
async def test_a_legacy_document_carries_its_catalog_time(monkeypatch):
    _authority(monkeypatch, native=False)
    conn = _LabelConn(legacy=[_catalog(A, "Alpha spec", "spec", ["x"], updated_at=T1)])

    labels = await _labels(conn, _doc(A))

    assert labels == {
        doc_uri(VAULT, A): {
            "name": "Alpha spec",
            "title": "Alpha spec",
            "doc_type": "spec",
            "tags": ["x"],
            "path": A,
            "updated_at": T1_STAMP,
        },
    }


@pytest.mark.asyncio
async def test_a_native_document_is_dated_by_its_resource_not_by_its_body(monkeypatch):
    """A body kept byte for byte from Git, or one that simply says so, states a
    time of its own; the moment the head was published is the resource's."""
    _authority(monkeypatch, native=True)
    conn = _LabelConn(
        native={A: _body("Alpha design", updated_at="1999-01-01T00:00:00Z", created_at="1998-01-01T00:00:00Z")},
        native_times={A: T1},
    )

    label = (await _labels(conn, _doc(A)))[doc_uri(VAULT, A)]

    assert label["updated_at"] == T1_STAMP
    assert label["name"] == "Alpha design"


@pytest.mark.asyncio
async def test_a_retained_catalog_row_never_dates_a_native_document(monkeypatch):
    """The row a document kept at cutover is frozen; the resource's time is not.
    It stays the answer whether the row says earlier or later."""
    _authority(monkeypatch, native=True)
    conn = _LabelConn(
        native={A: _body("Alpha"), B: _body("Beta")},
        native_times={A: T2, B: T1},
        legacy=[_catalog(A, "Frozen A", updated_at=T1), _catalog(B, "Frozen B", updated_at=T2)],
    )

    labels = await _labels(conn, _doc(A), _doc(B))

    assert labels[doc_uri(VAULT, A)]["updated_at"] == T2_STAMP
    assert labels[doc_uri(VAULT, B)]["updated_at"] == T1_STAMP


@pytest.mark.asyncio
async def test_a_pre_cutover_document_only_the_catalog_holds_is_dated_by_its_row(monkeypatch):
    _authority(monkeypatch, native=True)
    conn = _LabelConn(legacy=[_catalog(C, "Old note", "note", ["t"], updated_at=T1)])

    label = (await _labels(conn, _doc(C)))[doc_uri(VAULT, C)]

    assert label["updated_at"] == T1_STAMP


@pytest.mark.asyncio
async def test_a_wider_read_of_a_wide_frontmatter_keeps_the_resource_time(monkeypatch):
    """The second window re-selects the document; its time comes with it."""
    _authority(monkeypatch, native=True)
    conn = _LabelConn(
        native={"hub/index.md": _wide_body("Hub index", entries=300), A: _body("Alpha")},
        native_times={"hub/index.md": T1, A: T2},
    )

    labels = await _labels(conn, _doc("hub/index.md"), _doc(A))

    assert len(conn.native_windows) == 2  # the wide one really was read twice
    assert labels[doc_uri(VAULT, "hub/index.md")]["updated_at"] == T1_STAMP
    assert labels[doc_uri(VAULT, A)]["updated_at"] == T2_STAMP


@pytest.mark.asyncio
async def test_tables_and_files_are_dated_by_their_own_rows(monkeypatch):
    _authority(monkeypatch, native=False)
    file_id = str(uuid.uuid4())
    conn = _LabelConn(
        tables=[
            {"name": "metrics", "description": "Key metrics", "updated_at": T1},
            {"name": "plain", "description": None, "updated_at": T2},
        ],
        files=[{"id": file_id, "name": "diagram.png", "updated_at": T2}],
    )

    labels = await _labels(
        conn,
        (table_uri(VAULT, "metrics"), "table"),
        (table_uri(VAULT, "plain"), "table"),
        (file_uri(VAULT, file_id), "file"),
    )

    assert labels[table_uri(VAULT, "metrics")] == {
        "name": "Key metrics", "title": "Key metrics", "updated_at": T1_STAMP,
    }
    assert labels[table_uri(VAULT, "plain")]["updated_at"] == T2_STAMP
    assert labels[file_uri(VAULT, file_id)] == {
        "name": "diagram.png", "title": "diagram.png", "updated_at": T2_STAMP,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
async def test_a_resource_with_no_time_states_none(monkeypatch, native):
    """Absent, never null and never a made-up date: the row is the only source."""
    _authority(monkeypatch, native=native)
    file_id = str(uuid.uuid4())
    conn = _LabelConn(
        legacy=[] if native else [_catalog(A, "Alpha", updated_at=None)],
        native={A: _body("Alpha")} if native else None,  # no entry in native_times
        tables=[{"name": "metrics", "description": None, "updated_at": None}],
        files=[{"id": file_id, "name": "diagram.png"}],  # no `updated_at` key at all reads like a null
    )

    labels = await _labels(
        conn, _doc(A), (table_uri(VAULT, "metrics"), "table"), (file_uri(VAULT, file_id), "file"),
    )

    assert len(labels) == 3
    assert all("updated_at" not in label for label in labels.values()), labels


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
async def test_a_resource_the_vault_does_not_hold_states_no_time(monkeypatch, native):
    _authority(monkeypatch, native=native)
    conn = _LabelConn(
        legacy=[_catalog(B, "Other", updated_at=T1)],
        native={B: _body("Other")} if native else None,
        native_times={B: T1},
    )

    labels = await _labels(
        conn, _doc(A), (table_uri(VAULT, "ghost"), "table"), (file_uri(VAULT, "no-such-file"), "file"),
    )

    assert labels == {
        doc_uri(VAULT, A): {"name": A, "path": A},
        table_uri(VAULT, "ghost"): {"name": "ghost"},
        file_uri(VAULT, "no-such-file"): {"name": "no-such-file"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 7, 200])
@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
async def test_times_cost_no_statement_however_many_nodes(monkeypatch, native, count):
    """The time is one more column of the statements that already name the nodes."""
    _authority(monkeypatch, native=native)
    paths = [f"bulk/doc-{i}.md" for i in range(count)]
    names = [f"t{i}" for i in range(count)]
    file_ids = [str(uuid.uuid4()) for _ in range(count)]
    conn = _LabelConn(
        legacy=[] if native else [_catalog(p, f"Title {p}", updated_at=T1) for p in paths],
        native={p: _body(f"Title {p}") for p in paths} if native else None,
        native_times={p: T1 for p in paths},
        tables=[{"name": n, "description": None, "updated_at": T2} for n in names],
        files=[{"id": fid, "name": fid, "updated_at": T2} for fid in file_ids],
    )

    labels = await _labels(
        conn,
        *[_doc(p) for p in paths],
        *[(table_uri(VAULT, n), "table") for n in names],
        *[(file_uri(VAULT, fid), "file") for fid in file_ids],
    )

    assert len(labels) == 3 * count
    assert len(conn.queries) == 3, conn.queries  # documents, tables, files — as before
    assert {labels[doc_uri(VAULT, p)]["updated_at"] for p in paths} == {T1_STAMP}
    assert {labels[table_uri(VAULT, n)]["updated_at"] for n in names} == {T2_STAMP}
    assert {labels[file_uri(VAULT, fid)]["updated_at"] for fid in file_ids} == {T2_STAMP}


def test_every_label_statement_selects_the_time():
    """A guard on the SQL text itself: a column dropped from a statement would
    leave the fakes above agreeing with themselves."""
    assert "COALESCE(r.updated_at, dp.updated_at) AS updated_at" in kg_service._NATIVE_DOC_LABELS_SQL
    # The retained catalog row (`dn`) must never be the source of a native document's time.
    assert "dn.updated_at" not in kg_service._NATIVE_DOC_LABELS_SQL


# ── The label reaches every projection ───────────────────────────────────


def _use_connection(monkeypatch, conn) -> None:
    async def _pool():
        return _Pool(conn)

    monkeypatch.setattr(kg_service, "get_pool", _pool)


class _GraphConn(_LabelConn):
    """Edges for the projections, plus the label catalogs."""

    def __init__(self, *, edges=(), **kwargs) -> None:
        super().__init__(**kwargs)
        self.edges = list(edges)

    async def fetchrow(self, query: str, *_args):
        return {"edges_total": len(self.edges), "nodes_total": len({u for e in self.edges for u in e[:2]})}

    async def fetch(self, query: str, *args):
        if "FROM edges" in query or "FROM scoped_edges" in query:
            return self._edge_rows(query, args)
        return await super().fetch(query, *args)

    def _edge_rows(self, query: str, args):
        if "count(*) AS degree" in query:
            degree: dict[str, int] = {}
            for source, target, *_ in self.edges:
                degree[source] = degree.get(source, 0) + 1
                degree[target] = degree.get(target, 0) + 1
            floor = args[2] if "HAVING" in query else 0  # the hub query's `degree >= $3`
            return [
                {"uri": u, "rtype": "doc", "degree": d}
                for u, d in sorted(degree.items()) if d >= floor
            ]
        if "relation_type, kind" in query:
            return [self._row(e) for e in self.edges]
        if "source_uri = ANY" in query or "e.source_uri = $1" in query:
            seeds = args[0] if isinstance(args[0], list) else [args[0]]
            return [self._row(e) for e in self.edges if e[0] in seeds]
        seeds = args[0] if isinstance(args[0], list) else [args[0]]
        return [self._row(e) for e in self.edges if e[1] in seeds]

    @staticmethod
    def _row(edge) -> dict:
        source, target, relation = edge
        return {
            "source_uri": source, "target_uri": target, "target_type": "doc", "source_type": "doc",
            "relation_type": relation, "kind": "explicit", "stored_edge_type": None,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
async def test_a_relation_row_names_the_other_end_by_its_title(monkeypatch, native):
    _authority(monkeypatch, native=native)
    conn = _GraphConn(
        edges=[(doc_uri(VAULT, A), doc_uri(VAULT, B), "references")],
        legacy=[] if native else [_catalog(B, "Beta notes", "note", ["n"])],
        native={B: _body("Beta notes", tags=["n"])} if native else None,
    )
    _use_connection(monkeypatch, conn)

    (row,) = await kg_service.get_resource_relations(VAULT, doc_uri(VAULT, A), vault_id=VAULT_ID)

    assert row == {
        "direction": "outgoing", "relation": "references", "uri": doc_uri(VAULT, B),
        "resource_type": "doc", "kind": "explicit", "edge_type": "strong",
        "name": "Beta notes", "title": "Beta notes", "doc_type": "note", "tags": ["n"], "path": B,
    }
    assert len(conn.document_queries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
async def test_neighbourhood_and_overview_nodes_carry_the_label(monkeypatch, native):
    _authority(monkeypatch, native=native)
    edges = [(doc_uri(VAULT, A), doc_uri(VAULT, B), "references")]
    conn = _GraphConn(
        edges=edges,
        legacy=[] if native else [_catalog(A, "Alpha", "spec", ["a"]), _catalog(B, "Beta")],
        native={A: _body("Alpha", type="spec", tags=["a"]), B: _body("Beta")} if native else None,
    )
    _use_connection(monkeypatch, conn)

    graph = await kg_service.get_graph(VAULT, doc_uri(VAULT, A), hops=1, vault_id=VAULT_ID)
    overview = await kg_service.get_overview(VAULT, vault_id=VAULT_ID, include_orphans=False)

    for nodes in (graph["nodes"], overview["nodes"]):
        by_uri = {n["uri"]: n for n in nodes}
        assert by_uri[doc_uri(VAULT, A)]["name"] == "Alpha"
        assert by_uri[doc_uri(VAULT, A)]["title"] == "Alpha"
        assert by_uri[doc_uri(VAULT, A)]["doc_type"] == "spec"
        assert by_uri[doc_uri(VAULT, A)]["tags"] == ["a"]
        assert by_uri[doc_uri(VAULT, A)]["path"] == A
        assert by_uri[doc_uri(VAULT, B)]["name"] == "Beta"
        assert by_uri[doc_uri(VAULT, B)]["doc_type"] == "note"
    # Existing node fields are untouched.
    assert {n["uri"]: n["depth"] for n in graph["nodes"]} == {doc_uri(VAULT, A): 0, doc_uri(VAULT, B): 1}
    assert {n["degree"] for n in overview["nodes"]} == {1}


@pytest.mark.asyncio
async def test_the_overview_resolves_all_its_nodes_in_one_batch(monkeypatch):
    _authority(monkeypatch, native=True)
    hub = doc_uri(VAULT, "hub.md")
    spokes = [f"spokes/s-{i}.md" for i in range(60)]
    conn = _GraphConn(
        edges=[(hub, doc_uri(VAULT, s), "references") for s in spokes],
        native={"hub.md": _body("Hub"), **{s: _body(f"Spoke {s}") for s in spokes}},
    )
    _use_connection(monkeypatch, conn)

    overview = await kg_service.get_overview(VAULT, vault_id=VAULT_ID, include_orphans=False)

    assert len(overview["nodes"]) == 61
    assert {n["name"] for n in overview["nodes"] if n["uri"] != hub} == {f"Spoke {s}" for s in spokes}
    assert len(conn.document_queries) == 1


@pytest.mark.asyncio
async def test_health_hubs_carry_the_label_too(monkeypatch):
    _authority(monkeypatch, native=False)
    hub = doc_uri(VAULT, A)
    spokes = [doc_uri(VAULT, f"s/{i}.md") for i in range(5)]

    class _HealthConn(_GraphConn):
        async def fetch(self, query: str, *args):
            if "FROM documents WHERE vault_id" in query:  # the orphan scan
                return []
            return await super().fetch(query, *args)

    conn = _HealthConn(
        edges=[(hub, s, "references") for s in spokes],
        legacy=[_catalog(A, "Alpha spec", "spec", ["x"])],
    )
    _use_connection(monkeypatch, conn)

    health = await kg_service.get_health(VAULT, vault_id=VAULT_ID, hub_threshold=5)

    (top,) = health["hubs"]
    assert (top["uri"], top["name"], top["degree"]) == (hub, "Alpha spec", 5)
    assert (top["title"], top["doc_type"], top["tags"], top["path"]) == ("Alpha spec", "spec", ["x"], A)


@pytest.mark.asyncio
async def test_orphan_nodes_are_labelled_like_connected_ones(monkeypatch):
    class _OrphanConn:
        async def fetch(self, query: str, *_args):
            if "FROM documents" in query:
                return [{"path": A, "title": "Alpha spec", "doc_type": "spec", "tags": ["x"]},
                        {"path": B, "title": "", "doc_type": None, "tags": None}]
            if "FROM vault_tables" in query:
                return [{"name": "metrics", "description": "Key metrics", "coll": None}]
            return [{"id": "f1", "name": "diagram.png", "coll": None}]

    nodes, truncated = await kg_service._fetch_orphan_nodes(
        _OrphanConn(), VAULT, VAULT_ID, doc_paths=set(), table_names=set(), file_ids=set(), limit=10,
    )

    by_uri = {n["uri"]: n for n in nodes}
    assert truncated is False
    assert by_uri[doc_uri(VAULT, A)] == {
        "uri": doc_uri(VAULT, A), "resource_type": "doc", "degree": 0, "name": "Alpha spec",
        "title": "Alpha spec", "doc_type": "spec", "tags": ["x"], "path": A,
    }
    untitled = by_uri[doc_uri(VAULT, B)]
    assert (untitled["name"], untitled["doc_type"], untitled["tags"]) == (B, "note", [])
    assert "title" not in untitled
    assert by_uri[table_uri(VAULT, "metrics")]["title"] == "Key metrics"
    assert by_uri[file_uri(VAULT, "f1")]["title"] == "diagram.png"


# ── The time reaches every projection ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
async def test_a_relation_row_carries_the_time_of_the_other_end(monkeypatch, native):
    _authority(monkeypatch, native=native)
    conn = _GraphConn(
        edges=[(doc_uri(VAULT, A), doc_uri(VAULT, B), "references")],
        legacy=[] if native else [_catalog(B, "Beta notes", "note", ["n"], updated_at=T1)],
        native={B: _body("Beta notes", tags=["n"])} if native else None,
        native_times={B: T1},
    )
    _use_connection(monkeypatch, conn)

    (outgoing,) = await kg_service.get_resource_relations(VAULT, doc_uri(VAULT, A), vault_id=VAULT_ID)
    # Seen from the other end, the row describes A — which has no time in this world.
    (incoming,) = await kg_service.get_resource_relations(VAULT, doc_uri(VAULT, B), vault_id=VAULT_ID)

    assert outgoing == {
        "direction": "outgoing", "relation": "references", "uri": doc_uri(VAULT, B),
        "resource_type": "doc", "kind": "explicit", "edge_type": "strong",
        "name": "Beta notes", "title": "Beta notes", "doc_type": "note", "tags": ["n"], "path": B,
        "updated_at": T1_STAMP,
    }
    assert incoming["uri"] == doc_uri(VAULT, A) and "updated_at" not in incoming
    assert len(conn.document_queries) == 2  # one batch per call, never one per row


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
async def test_neighbourhood_and_overview_nodes_carry_their_own_times(monkeypatch, native):
    _authority(monkeypatch, native=native)
    ghost = doc_uri(VAULT, "gone/ghost.md")
    conn = _GraphConn(
        edges=[
            (doc_uri(VAULT, A), doc_uri(VAULT, B), "references"),
            (doc_uri(VAULT, A), ghost, "references"),
        ],
        legacy=[] if native else [
            _catalog(A, "Alpha", "spec", ["a"], updated_at=T2), _catalog(B, "Beta", updated_at=T1),
        ],
        native={A: _body("Alpha", type="spec", tags=["a"]), B: _body("Beta")} if native else None,
        native_times={A: T2, B: T1},
    )
    _use_connection(monkeypatch, conn)

    graph = await kg_service.get_graph(VAULT, doc_uri(VAULT, A), hops=1, vault_id=VAULT_ID)
    overview = await kg_service.get_overview(VAULT, vault_id=VAULT_ID, include_orphans=False)

    for nodes in (graph["nodes"], overview["nodes"]):
        by_uri = {n["uri"]: n for n in nodes}
        assert by_uri[doc_uri(VAULT, A)]["updated_at"] == T2_STAMP
        assert by_uri[doc_uri(VAULT, B)]["updated_at"] == T1_STAMP
        # An edge whose document is gone is still drawn, and claims no time.
        assert "updated_at" not in by_uri[ghost]
    # Existing node fields are untouched.
    assert {n["uri"]: n["depth"] for n in graph["nodes"]} == {
        doc_uri(VAULT, A): 0, doc_uri(VAULT, B): 1, ghost: 1,
    }
    assert {n["degree"] for n in overview["nodes"]} == {1, 2}


@pytest.mark.asyncio
async def test_the_overview_dates_all_its_nodes_in_the_one_batch(monkeypatch):
    _authority(monkeypatch, native=True)
    hub = doc_uri(VAULT, "hub.md")
    spokes = [f"spokes/s-{i}.md" for i in range(60)]
    conn = _GraphConn(
        edges=[(hub, doc_uri(VAULT, s), "references") for s in spokes],
        native={"hub.md": _body("Hub"), **{s: _body(f"Spoke {s}") for s in spokes}},
        native_times={"hub.md": T2, **{s: T1 for s in spokes}},
    )
    _use_connection(monkeypatch, conn)

    overview = await kg_service.get_overview(VAULT, vault_id=VAULT_ID, include_orphans=False)

    assert len(overview["nodes"]) == 61
    assert {n["updated_at"] for n in overview["nodes"] if n["uri"] != hub} == {T1_STAMP}
    assert next(n for n in overview["nodes"] if n["uri"] == hub)["updated_at"] == T2_STAMP
    assert len(conn.document_queries) == 1


@pytest.mark.asyncio
async def test_health_hubs_carry_their_time_too(monkeypatch):
    _authority(monkeypatch, native=False)
    hub = doc_uri(VAULT, A)
    spokes = [doc_uri(VAULT, f"s/{i}.md") for i in range(5)]

    class _HealthConn(_GraphConn):
        async def fetch(self, query: str, *args):
            if "FROM documents WHERE vault_id" in query:  # the orphan scan
                return []
            return await super().fetch(query, *args)

    conn = _HealthConn(
        edges=[(hub, s, "references") for s in spokes],
        legacy=[_catalog(A, "Alpha spec", "spec", ["x"], updated_at=T1)],
    )
    _use_connection(monkeypatch, conn)

    health = await kg_service.get_health(VAULT, vault_id=VAULT_ID, hub_threshold=5)

    (top,) = health["hubs"]
    assert (top["uri"], top["name"], top["degree"], top["updated_at"]) == (hub, "Alpha spec", 5, T1_STAMP)


class _OrphanRows:
    """Answers the three orphan statements and records the SQL it was sent."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetch(self, query: str, *_args):
        self.queries.append(query)
        if "FROM documents" in query:
            return [
                {"path": A, "title": "Alpha spec", "doc_type": "spec", "tags": ["x"], "updated_at": T1},
                {"path": B, "title": "", "doc_type": None, "tags": None, "updated_at": None},
            ]
        if "FROM vault_tables" in query:
            return [{"name": "metrics", "description": "Key metrics", "updated_at": T2, "coll": None}]
        return [{"id": "f1", "name": "diagram.png", "updated_at": T2, "coll": None}]


@pytest.mark.asyncio
async def test_orphan_nodes_carry_the_time_of_their_row(monkeypatch):
    _authority(monkeypatch, native=False)
    conn = _OrphanRows()

    nodes, _truncated = await kg_service._fetch_orphan_nodes(
        conn, VAULT, VAULT_ID, doc_paths=set(), table_names=set(), file_ids=set(), limit=10,
    )

    by_uri = {n["uri"]: n for n in nodes}
    assert by_uri[doc_uri(VAULT, A)]["updated_at"] == T1_STAMP
    assert "updated_at" not in by_uri[doc_uri(VAULT, B)]  # its row had none
    assert by_uri[table_uri(VAULT, "metrics")]["updated_at"] == T2_STAMP
    assert by_uri[file_uri(VAULT, "f1")]["updated_at"] == T2_STAMP
    assert len(conn.queries) == 3  # documents, tables, files: no lookup per orphan


@pytest.mark.asyncio
async def test_orphan_documents_follow_the_installation_authority(monkeypatch):
    """Legacy never names a native table; native lets the ledger's time win over
    the catalog row that survived cutover, in the same statement."""
    _authority(monkeypatch, native=False)
    legacy = _OrphanRows()
    await kg_service._fetch_orphan_nodes(
        legacy, VAULT, VAULT_ID, doc_paths=set(), table_names=set(), file_ids=set(), limit=10,
    )
    assert not any("native_" in query for query in legacy.queries)

    _authority(monkeypatch, native=True)
    native = _OrphanRows()
    await kg_service._fetch_orphan_nodes(
        native, VAULT, VAULT_ID, doc_paths=set(), table_names=set(), file_ids=set(), limit=10,
    )
    doc_query = next(q for q in native.queries if "FROM documents" in q)
    assert "COALESCE(r.updated_at, d.updated_at) AS updated_at" in doc_query
    assert "r.resource_id = d.id" in doc_query and "r.lifecycle = 'live'" in doc_query
    assert len(native.queries) == 3
