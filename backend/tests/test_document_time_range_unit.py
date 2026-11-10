from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError as ModelError

from app.exceptions import ValidationError
from app.services.search_filters import DocumentTimeRange
from app.services.search_service import SearchService


START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("bounds", [{}, {"gte": START, "lt": START}, {"gte": START, "lt": START - timedelta(seconds=1)},
                                    {"gte": START.replace(tzinfo=None)}, {"field": "indexed_at", "lt": START}])
def test_invalid_window(bounds):
    with pytest.raises(ModelError):
        DocumentTimeRange.model_validate({"field": "updated_at", **bounds})


def test_half_open_instants_and_offsets():
    local = START.astimezone(timezone(timedelta(hours=9)))
    end = START + timedelta(microseconds=1)
    window = DocumentTimeRange(field="created_at", gte=local, lt=end)
    assert window.gte.tzinfo == UTC and window.includes(START) and not window.includes(end)
    assert DocumentTimeRange(field="updated_at", lt=end).includes(START)


@pytest.mark.parametrize("kind", [None, "table", "file"])
async def test_time_range_requires_a_document_search(kind):
    with pytest.raises(ValidationError, match="source_type=document"):
        await SearchService().search("x", vault="v", source_type=kind, time_range=DocumentTimeRange(field="created_at", gte=START))
