from app.services.knowledge_io import _is_reserved_record


def test_reserved_by_collection():
    assert _is_reserved_record({"collection": "overview", "type": "note"}) is True
    assert _is_reserved_record({"collection": "overview/x", "type": "note"}) is True


def test_reserved_by_type():
    assert _is_reserved_record({"collection": "notes", "type": "skill"}) is True


def test_normal_record_passes():
    assert _is_reserved_record({"collection": "notes", "type": "note"}) is False


def test_metadata_worker_cannot_assign_skill():
    from app.services.metadata_worker import _DOC_TYPES

    assert "skill" not in _DOC_TYPES
