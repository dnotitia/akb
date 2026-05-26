from app.services.sparse_encoder import _english_token_variants


def test_english_token_variants_keep_original_and_match_past_tense():
    assert "graduate" in _english_token_variants("graduated")
    assert "graduat" in _english_token_variants("graduate")


def test_english_token_variants_match_regular_past_tense():
    assert "repaint" in _english_token_variants("repainted")


def test_english_token_variants_match_simple_plural():
    assert "wall" in _english_token_variants("walls")


def test_english_token_variants_drop_common_stopwords():
    assert _english_token_variants("what") == []
    assert _english_token_variants("did") == []
    assert _english_token_variants("my") == []


def test_english_token_variants_leave_non_ascii_untouched():
    assert _english_token_variants("쿠버네티스") == ["쿠버네티스"]


def test_english_token_variants_stem_plural_once_no_es_s_overlap():
    # "es" and bare "s" rules must be mutually exclusive — no "churche" noise.
    churches = _english_token_variants("churches")
    assert "church" in churches
    assert "churche" not in churches
    # "-ies" preempts the "-es"/"-s" rules — no "studi"/"studie" noise.
    studies = _english_token_variants("studies")
    assert "study" in studies
    assert "studi" not in studies
    assert "studie" not in studies
