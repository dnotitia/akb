"""How close `bm25_term_id_seq` is to the id the `vchord` index cannot hold.

The index holds term ids 0 .. 2^30 - 1, and a document holding a larger one is
refused (akb#691). Once the sequence passes that, every document with a new
term is refused. `/health` reports the distance, and warns while there is still
time to renumber (`scripts/compact_bm25_term_ids.py`).
"""

from __future__ import annotations

import pytest

from app.services import sparse_encoder

_LIMIT = 1 << 30


def test_the_headroom_is_the_last_id_drawn_over_the_index_limit():
    headroom = sparse_encoder.term_id_headroom(728_985_612, vchord=True)

    assert headroom["last_drawn"] == 728_985_612
    assert headroom["vchord_limit"] == _LIMIT
    assert headroom["used"] == pytest.approx(728_985_612 / _LIMIT, abs=1e-4)
    assert "warning" not in headroom


def test_the_warning_starts_at_ninety_percent_and_names_the_remedy():
    below = sparse_encoder.term_id_headroom(int(_LIMIT * 0.9) - 1, vchord=True)
    at = sparse_encoder.term_id_headroom(int(_LIMIT * 0.9) + 1, vchord=True)

    assert "warning" not in below
    assert "compact_bm25_term_ids" in at["warning"]
    assert "90" in at["warning"]


def test_only_the_vchord_shape_is_warned():
    """The other shapes store ids as bigint; 2^30 is the vchord index's limit."""
    assert "warning" not in sparse_encoder.term_id_headroom(_LIMIT, vchord=False)


def test_a_sequence_that_has_drawn_nothing_is_empty():
    assert sparse_encoder.term_id_headroom(0, vchord=True)["used"] == 0
