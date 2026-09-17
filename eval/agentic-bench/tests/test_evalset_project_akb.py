"""The tracked seed evalset must match the schema the harness reads.

`evalset/` itself is private (hand-authored Korean-law ground truth), so
these three project-akb questions are the only worked examples of the
schema in the repository. If they drift from what `judge.py` reads, the
schema documentation in the README drifts with them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

EVALSET = Path(__file__).resolve().parents[1] / "evalset-project-akb"
QUESTIONS = sorted(EVALSET.glob("q*.yaml"))


def test_the_seed_set_has_its_three_questions():
    assert [p.stem for p in QUESTIONS] == ["q001", "q002", "q003"]


@pytest.mark.parametrize("path", QUESTIONS, ids=lambda p: p.stem)
def test_question_matches_the_readme_schema(path):
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert set(spec) == {"id", "category", "query", "ground_truth"}
    assert spec["id"] == path.stem
    assert isinstance(spec["category"], str) and spec["category"]
    assert isinstance(spec["query"], str) and spec["query"].strip()

    truth = spec["ground_truth"]
    assert set(truth) == {"must_mention", "forbidden", "source_docs"}
    for key in truth:
        assert isinstance(truth[key], list), key
        assert all(isinstance(item, str) and item for item in truth[key]), key

    assert truth["must_mention"], "a question with nothing to assert scores nothing"
    assert truth["source_docs"], "every question must name where its answer lives"
    for uri in truth["source_docs"]:
        assert uri.startswith("akb://project-akb/"), uri


def test_the_judge_can_load_the_seed_set_through_evalset_dir(monkeypatch):
    # `judge.py` resolves EVALSET at import time from EVALSET_DIR, which is
    # how the seed set is selected without moving files around.
    monkeypatch.setenv("EVALSET_DIR", str(EVALSET))
    import importlib

    import src.judge as judge

    reloaded = importlib.reload(judge)
    try:
        assert reloaded.EVALSET == EVALSET
        assert reloaded.list_query_ids() == ["q001", "q002", "q003"]
    finally:
        monkeypatch.delenv("EVALSET_DIR", raising=False)
        importlib.reload(judge)
