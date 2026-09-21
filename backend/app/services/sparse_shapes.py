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

from typing import Literal, get_args

SparseShape = Literal["posting", "arrays", "vchord"]

#: `posting` is the production shape: a side table of (term, document, weight)
#: rows. `arrays` keeps the same numbers in columns on `chunks` and exists for
#: the bench harness. `vchord` hands the terms to a block-max BM25 index inside
#: PostgreSQL and lets it own the scoring, which also changes the weight
#: convention the encoder must produce — see `sparse_encoder._use_raw_weights`.
#:
#: Every member, derived from the type rather than restated beside it. Anything
#: that needs to enumerate shapes (tests, validation, an operator-facing list)
#: reads this, so a new member cannot be added to one and missed by the other.
SPARSE_SHAPES: tuple[SparseShape, ...] = get_args(SparseShape)
