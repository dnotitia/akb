"""Native activity filters and migrated revision exclusions run before LIMIT."""
from datetime import UTC, datetime, timedelta
import uuid

from app.services import native_revision_service
from app.services.native_revision_backend import NativeRevisionBackend
from tests.test_event_tail_postgres import _fresh_database, _make_vault


async def test_native_activity_window_and_ties(monkeypatch):
    start = datetime(2026, 4, 1, tzinfo=UTC)
    stamp = start

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return stamp

    monkeypatch.setattr(native_revision_service, "datetime", Clock)
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "activity")
        native = native_revision_service.NativeRevisionService(pool)
        ids = []
        for index, (collection, actor, hour) in enumerate([
            ("notes_x", "김Writer", 0), ("notes_x", "김Writer", 1), ("notes_x", "김Writer", 1),
            ("notesAx", "writer", 1), ("notes_x", "writer", 2), ("notes_x", "other", 3),
        ]):
            stamp = start + timedelta(hours=hour)
            doc = await native.create_text(namespace_id=vault, surface="document", path=f"{collection}/{index}.md",
                                           payload="body", actor=actor, mutation_id=uuid.uuid4())
            ids.append(doc.revision_id)
        backend = NativeRevisionBackend(pool=pool)
        async def history(**options):
            args = {"max_count": 20, "since": None, "path": "notes_x", "author": "WrItEr", **options}
            return [e["hash"] for e in await backend.vault_activity("activity", **args)]
        end = start + timedelta(hours=2)
        ties = sorted(ids[1:3], reverse=True)
        assert await history(since=start.date().isoformat(), until=end) == [*ties, ids[0]]
        assert await history(until=start) == []
        assert await history(until=end + timedelta(microseconds=1), max_count=1) == [ids[4]]
        assert await history(author="김", max_count=1) == ties[:1]
        assert await history(author="%") == []
