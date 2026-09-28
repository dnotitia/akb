"""How the pgvector driver stores BM25 terms — declared once.

This was two `Literal`s: one in `app/config.py` for the setting, one in
`app/services/vector_store/pgvector.py` for the driver argument. Same members,
different order, no link — so adding a member to one and not the other
type-checks cleanly and then behaves as whatever the other file's `else` branch
happens to be (akb#623).

It lives here rather than in the driver because `pgvector.py` deliberately does
not import `app.config`: the shape arrives as a constructor argument, which is
what keeps the driver independent of how the process is configured. And it
cannot live inside `app/services/vector_store/` because that package's
`__init__` imports the factory, which imports config — so config importing it
back would close a cycle. `app/__init__.py` and `app/services/__init__.py` are
both empty, which is what makes this module safe for either side to import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

SparseShape = Literal["posting", "arrays", "vchord"]

#: `vchord` hands the terms to a block-max BM25 index inside PostgreSQL and lets
#: it own the scoring, which also changes the weight convention the encoder must
#: produce — see `sparse_encoder._use_raw_weights`. It is what a new database
#: gets by default where the server provides the `vchord_bm25` extension.
#: `posting` is a side table of (term, document, weight) rows: the shape every
#: installation used before, and the one a server without the extension gets.
#: `arrays` keeps the same numbers in columns on `chunks` and exists for the
#: bench harness.
#:
#: Every member, derived from the type rather than restated beside it. Anything
#: that needs to enumerate shapes (tests, validation, an operator-facing list)
#: reads this, so a new member cannot be added to one and missed by the other.
SPARSE_SHAPES: tuple[SparseShape, ...] = get_args(SparseShape)

#: What the setting accepts: every shape, plus `auto`, the default. `auto` is
#: not a shape. Startup decides it once per database, before the store is built,
#: and records what it decided (`vector_store/sparse_shape_state.py`); nothing
#: may read an undecided `auto` as a shape. Nested, so the shapes are still
#: declared once — `typing` flattens a `Literal` inside a `Literal`.
SparseShapeSetting = Literal["auto", SparseShape]

#: How a database's shape was arrived at, in the order startup asks.
DecidedBy = Literal[
    # The setting names a shape: nothing was decided.
    "configured",
    # A previous start recorded the shape this database serves.
    "recorded",
    # No record, but the database already holds a shape's tables. A `posting`
    # table wins over a BM25 index: an installation that has served `posting`
    # moves to `vchord` only through the backfill runbook and an explicit
    # setting, never because an index happens to exist.
    "existing_posting",
    "existing_bm25_index",
    "existing_arrays",
    # Nothing there yet: `vchord` if the server can provide the extension to
    # this role, `posting` otherwise.
    "new_database",
]


@dataclass(frozen=True)
class SparseShapeDecision:
    """The shape a pgvector database serves, and what startup learned deciding it.

    `posting_table_present` travels with it because the external-statistics
    policy needs it too: under `vchord`, a `posting` table is a way back that
    must be kept current, and without one nothing reads those statistics."""

    shape: SparseShape
    decided_by: DecidedBy
    posting_table_present: bool
    #: One sentence for the startup log and `/health`: why, and how to change it.
    note: str = ""
