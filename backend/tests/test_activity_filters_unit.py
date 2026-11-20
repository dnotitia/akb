"""Activity selection through the manual Git, fixed-ref and sealed mirror readers."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from git import Actor, Repo
from jsonschema import Draft202012Validator
import pytest

from app.services.git_service import GitService


@pytest.fixture
def commits(tmp_path):
    path = tmp_path / "working"
    repo = Repo.init(path)
    start = datetime(2026, 4, 1, tzinfo=UTC)
    ids = []
    for index, (collection, author, agent, hour) in enumerate([
        ("notes_x", "김Writer", "bot", 0), ("notes_x", "bot", "편집Writer", 1),
        ("notesAx", "writer", "writer", 1), ("notes_x", "other", "other", 1),
        ("notes_x", "other", "other", 2), ("notes_x", "other", "other", 3),
    ]):
        file = path / collection / "doc.md"
        file.parent.mkdir(exist_ok=True)
        file.write_text(str(index))
        repo.index.add([str(file.relative_to(path))])
        date = start + timedelta(hours=hour)
        identity = Actor(author, "user@example.invalid")
        ids.append(repo.index.commit(f"Change {index}\n\nagent: {agent}", author=identity,
                                     committer=identity, author_date=date, commit_date=date).hexsha)
    repo.close()
    return path, start, ids


@pytest.mark.parametrize("reader", ["manual", "mirror", "fixed"])
def test_activity_filters_precede_limit(commits, tmp_path, monkeypatch, reader):
    from app.services import external_git_runner
    path, start, ids = commits
    service = GitService(storage_path=str(tmp_path / "vaults"))
    monkeypatch.setattr(service, "_get_repo", lambda name: Repo(path))
    monkeypatch.setattr(service, "_use_mirror_reader", lambda name: reader == "mirror")
    monkeypatch.setattr(service, "_mirror_bare", lambda name: path / ".git")
    monkeypatch.setattr(external_git_runner, "_ACTIVITY_METADATA_PAGE_SIZE", 2)

    def events(**options):
        kw = {"max_count": 2, "since": None, "path": "notes_x", **options}
        if reader == "fixed":
            return service.manual_fixed_ref_vault_log("v", ids[-1], **kw)
        return service.vault_log("v", **kw)

    matched = events(author="wRiTeR")
    assert [e["hash"] for e in matched] == [ids[1][:12], ids[0][:12]]
    assert all(e["files"] == [{"path": "notes_x/doc.md", "change": change}]
               for e, change in zip(matched, ["modified", "added"], strict=True))
    assert events(author="missing") == []
    end = start + timedelta(hours=1)
    assert events(author="writer", since=start.isoformat(), until=end) == matched[1:]
    assert events(author="writer", since=(start + timedelta(microseconds=1)).isoformat(),
                  until=end + timedelta(microseconds=1)) == matched[:1]
    assert events(until=start) == []
    assert events(until=datetime(1970, 1, 1, tzinfo=UTC)) == []
    assert events(until=start + timedelta(microseconds=1)) == matched[1:]
    assert [e["hash"] for e in events()] == [ids[-1][:12], ids[-2][:12]]


@pytest.mark.parametrize(("options", "field"), [
    ({"until": None}, "/until"), ({"until": "2026-04-01"}, "/until"),
    ({"until": "2026-04-01T00:00:00"}, "/until"), ({"until": "0001-01-01T00:00:00+01:00"}, "/until"),
    ({"since": "bad", "until": "2026-04-01T00:00:00Z"}, "/since"),
    ({"since": "0001-01-01T00:00:00+01:00", "until": "2026-04-01T00:00:00Z"}, "/since"),
    ({"since": "2026-04-01", "until": "2026-04-01T09:00:00+09:00"}, "/until"),
    ({"since": "2026-05-01", "until": "2026-04-01T00:00:00Z"}, "/until"),
    ({"limit": 0}, "/limit"), ({"limit": True}, "/limit"), ({"author": 12}, "/author"),
    ({"collection": []}, "/collection"), ({"since": 42}, "/since"),
])
async def test_invalid_activity_filters_never_run_a_broader_read(tmp_path, monkeypatch, options, field):
    from app.config import settings
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    from mcp_server import server
    from mcp_server.tools import available_tools
    schema = next(t.input_schema for t in available_tools() if t.name == "akb_activity")
    assert Draft202012Validator(schema).is_valid({"vault": "v", "since": "2026-04-01"})
    read = AsyncMock()
    monkeypatch.setattr(server, "check_vault_access", AsyncMock())
    monkeypatch.setattr(server.revision_backend, "vault_activity", read)
    result = await server._handle_activity({"vault": "v", **options}, "reader", None)
    assert result["code"] == "invalid_argument" and result["details"]["field"] == field
    read.assert_not_awaited()
