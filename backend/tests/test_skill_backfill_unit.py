from app.services.skill_reservation_backfill import classify_violation, _move_target


def test_ordinary_doc_in_overview():
    row = {"path": "overview/notes.md", "doc_type": "note"}
    assert classify_violation(row) == "move_out"


def test_stray_skill_type():
    row = {"path": "notes/guide.md", "doc_type": "skill"}
    assert classify_violation(row) == "retype"


def test_canonical_retyped():
    row = {"path": "overview/vault-skill.md", "doc_type": "note"}
    assert classify_violation(row) == "restore_type"


def test_canonical_correct_is_clean():
    row = {"path": "overview/vault-skill.md", "doc_type": "skill"}
    assert classify_violation(row) is None


def test_normal_doc_clean():
    row = {"path": "notes/a.md", "doc_type": "note"}
    assert classify_violation(row) is None


def test_move_target_nested():
    assert _move_target("overview/a/b.md") == ("a", "b")


def test_move_target_root():
    assert _move_target("overview/x.md") == (None, "x")
