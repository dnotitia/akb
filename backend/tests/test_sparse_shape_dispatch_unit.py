"""A shape the code does not handle must fail loudly, not pick a side.

`vector_store_sparse_shape` used to be branched on as a two-way `if` in four
places, and the two files that branch disagreed about which way the default
went: the driver read "arrays, or else posting", the stats sampler read
"posting, or else arrays". A third member would have been two different shapes
at once — rows in a side table the size reporter had been told not to look at,
with nothing anywhere complaining (akb#623).

These tests add a member that nothing handles and assert that every branch
refuses it. They are the reason the members are declared once: a test that
restated the list would pass while the real code and the real setting drifted.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import get_args

import pytest

from app.config import Settings
from app.services.sparse_shapes import SPARSE_SHAPES, SparseShape, SparseShapeSetting
from app.services.vector_store import pgvector


def test_the_members_are_declared_once():
    """The setting and the driver argument read the same shapes, not two copies.

    The setting accepts exactly one value more than the driver: `auto`, which
    startup turns into a shape before any store is built. Anything else the
    setting accepted and the driver did not would be a value with no branch."""
    assert SPARSE_SHAPES == get_args(SparseShape)
    field = Settings.model_fields["vector_store_sparse_shape"]
    # Equality, not identity — and deliberately, because identity cannot tell
    # the two apart. `typing` caches `Literal`, so a restated
    # `Literal["posting", "arrays"]` IS the shared object. What this catches is
    # the thing that actually hurts: the two disagreeing about members.
    assert field.annotation is SparseShapeSetting
    assert get_args(field.annotation) == ("auto", *get_args(SparseShape)), (
        "설정과 드라이버가 서로 다른 멤버를 보고 있다 — 한쪽에 더하면 "
        "다른 쪽은 조용히 폴백한다"
    )
    assert pgvector.SparseShape is SparseShape


def test_the_setting_does_not_restate_the_members():
    """Caught structurally, since the runtime cannot see the difference.

    A restated literal agrees until somebody edits one of them, and then the
    disagreement is exactly what the previous test would report — after the
    fact. Refusing the restatement is how it stays a single declaration."""
    src = (Path(__file__).resolve().parents[1] / "app" / "config.py").read_text()
    line = next(ln for ln in src.splitlines() if "vector_store_sparse_shape:" in ln)
    assert "Literal[" not in line, (
        f"설정이 멤버를 다시 적고 있다: {line.strip()} — "
        f"app/services/sparse_shapes.SparseShape 를 쓸 것"
    )


def test_the_settings_model_refuses_an_unknown_shape():
    with pytest.raises(Exception):
        Settings(document_revision_backend="bare_git", vector_store_sparse_shape="no-such-shape")  # type: ignore[arg-type]


@pytest.mark.parametrize("site", ["_do_ensure", "upsert_one", "_search_sparse"])
def test_every_driver_branch_names_every_shape_and_ends_in_assert_never(site):
    """Structural, because the alternative is standing up PostgreSQL to learn
    that a branch fell through — and the fall-through is silent by nature."""
    src = inspect.getsource(getattr(pgvector.PgvectorStore, site))
    if "_sparse_shape" not in src:
        pytest.skip(f"{site} 는 모양으로 분기하지 않는다")
    for shape in SPARSE_SHAPES:
        assert f'_sparse_shape == "{shape}"' in src, (
            f"{site} 가 '{shape}' 를 명시적으로 다루지 않는다 — else 로 흘러간다"
        )
    assert "assert_never(self._sparse_shape)" in src, (
        f"{site} 에 소진 검사가 없다 — 미처리 모양이 조용히 한쪽으로 떨어진다"
    )


def test_the_stats_sampler_names_every_shape():
    from app.stats import sampler

    src = inspect.getsource(sampler.pgvector_relations)
    for shape in SPARSE_SHAPES:
        # The member has to be named; whether by `==` or by membership in a
        # tuple is the author's business. What is not allowed is a shape that
        # reaches the end without ever being mentioned.
        assert f'"{shape}"' in src, f"sampler 가 '{shape}' 를 안 다룬다"
    assert "assert_never(shape)" in src


@pytest.mark.parametrize("shape", SPARSE_SHAPES)
def test_driver_and_sampler_agree_on_the_relations_a_shape_creates(shape, monkeypatch):
    """The disagreement that motivated this: the driver would store rows in a
    relation the sampler never counted, and `vector_bytes` would be quietly
    wrong rather than absent."""
    from app.config import settings
    from app.stats import sampler

    monkeypatch.setattr(settings, "vector_store_sparse_shape", shape)
    counted = set(sampler.pgvector_relations())

    ddl = inspect.getsource(pgvector.PgvectorStore._do_ensure)
    branch = ddl.split(f'_sparse_shape == "{shape}"', 1)[1]
    for other in SPARSE_SHAPES:
        if other != shape:
            branch = branch.split(f'_sparse_shape == "{other}"', 1)[0]
    creates_posting = "CREATE TABLE IF NOT EXISTS" in branch and ".posting" in branch

    assert ("posting" in counted) == creates_posting, (
        f"'{shape}' 모양: 드라이버는 posting 을 "
        f"{'만든다' if creates_posting else '안 만든다'} 는데 "
        f"sampler 는 {'센다' if 'posting' in counted else '안 센다'}"
    )


async def test_the_posting_weights_kept_under_vchord_are_the_ones_posting_writes(monkeypatch):
    """A way back must serve what `posting` would have written (akb#615).

    While the rollback is retained, the vchord shape keeps `posting` current
    from the raw term frequencies it already encoded, rather than tokenizing a
    second time. That is only a way back if those weights are the ones the
    posting shape writes for the same text."""
    from app.services import sparse_encoder

    async def tokenize(text):
        return ["a", "b", "b", "c", "c", "c"]

    async def term_ids(terms):
        return {t: i for i, t in enumerate(sorted(terms), start=10)}

    async def stats():
        return {"total_docs": 100, "avgdl": 4.0, "k1": 1.2, "b": 0.75}

    monkeypatch.setattr(sparse_encoder, "tokenize", tokenize)
    monkeypatch.setattr(sparse_encoder, "get_or_create_term_ids", term_ids)
    monkeypatch.setattr(sparse_encoder, "load_stats", stats)

    posting_ids, posting_weights = await sparse_encoder.encode_document("x", sparse_shape="posting")
    raw_ids, raw_tfs = await sparse_encoder.encode_document("x", sparse_shape="vchord")

    assert raw_ids == posting_ids
    assert await sparse_encoder.saturate_for_posting(raw_tfs) == pytest.approx(posting_weights)


@pytest.mark.parametrize(
    ("shape", "mode", "keeps"),
    [
        ("vchord", "required", True),
        ("vchord", "vchord_only_verified", False),
        ("posting", "required", False),
    ],
)
def test_vchord_gets_posting_weights_only_while_the_way_back_is_retained(
    shape, mode, keeps, monkeypatch,
):
    """`required` keeps posting's statistics for a rollback; the rows follow it."""
    from app.config import settings
    from app.services import sparse_encoder
    from app.services.vector_store import factory

    monkeypatch.setattr(settings, "vector_store_driver", "pgvector")
    monkeypatch.setattr(settings, "vector_store_sparse_shape", shape)
    monkeypatch.setattr(settings, "bm25_external_stats_mode", mode)
    factory.reset_singleton_for_tests()
    try:
        store = factory.get_vector_store()
        assert (store._posting_weights is sparse_encoder.saturate_for_posting) is keeps
    finally:
        factory.reset_singleton_for_tests()
