"""Check public grep receipts against the stored document bodies and revisions."""
import pytest

from app.config import settings
from app.models.document import DocumentPutRequest
from app.services import skill_policy
from app.services.document_service import DocumentService
from app.services.git_service import GitService
from app.services.native_document_service import NativeDocumentService
from tests.test_event_tail_postgres import _fresh_database, _make_vault


@pytest.mark.parametrize("backend", ["postgres_native", "bare_git"])
async def test_writer_grep_reads_and_partial_replacement_match_storage(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "document_revision_backend", backend)
    from mcp_server import server

    vault = "grep-policy"
    pattern = "catalog marker"
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, vault)
        writer = await pool.fetchval(
            "INSERT INTO users(username,email,password_hash) VALUES('writer','writer@example.invalid','disabled') RETURNING id"
        )
        await pool.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'writer')", vault_id, writer)
        if backend == "bare_git":
            git = GitService(storage_path=str(tmp_path))
            git.init_vault(vault)
            docs = DocumentService(git=git)
        else:
            docs = NativeDocumentService(pool=pool)
        monkeypatch.setattr(server, "doc_service", docs)
        user = server._MCPUser(str(writer), "writer")

        # Place an ordinary document on each side of the guide in path order.
        paths = ["drafts/start.md", skill_policy.VAULT_SKILL_PATH, "reports/end.md"]
        created = []
        for path in paths:
            collection, filename = path.split("/")
            is_guide = path == skill_policy.VAULT_SKILL_PATH
            receipt = await docs.put(DocumentPutRequest(
                vault=vault, collection=collection, slug=filename.removesuffix(".md"),
                title=filename, type="skill" if is_guide else "note", content=f"Keep {pattern} here.",
            ), skill_internal=is_guide)
            created.append(receipt)
        first, guide, last = created

        async def grep(**args):
            return await server._dispatch("akb_grep", {"vault": vault, "pattern": pattern, **args}, user)

        async def stored():
            state = {}
            for path in paths:
                doc = await docs.get(vault, path)
                state[doc.uri] = (doc.content, doc.current_commit)
            return state

        before = await stored()
        matches = await grep()
        assert [row["uri"] for row in matches["results"]] == [first.uri, guide.uri, last.uri]
        assert matches["total_matches"] == 3
        assert (await grep(count_only=True))["by_doc"] == dict.fromkeys(before, 1)
        assert (await grep(files_with_matches=True))["files"] == list(before)
        assert await stored() == before

        # The preview limit hides the guide, but it is still in the write scope.
        partial = await grep(replace="", limit=1)
        assert partial["code"] == "permission_denied"
        assert partial["total_docs"] == 3 and partial["returned_docs"] == 1
        assert partial["replacement_complete"] is False and partial["replaced_docs"] == 1
        assert partial["details"]["failed_uri"] == guide.uri
        assert partial["details"]["committed_replacements"] == 1
        committed, = partial["replacements"]
        assert committed["uri"] == first.uri
        assert committed["previous_commit"] == before[first.uri][1]
        assert committed["commit"] != committed["previous_commit"]
        after = await stored()
        assert after == {**before, first.uri: ("Keep  here.", committed["commit"])}

        # With only the guide in scope the error has no committed prefix.
        denied = await grep(replace="updated", collection="overview")
        assert denied["code"] == "permission_denied"
        assert denied["details"]["failed_uri"] == guide.uri
        assert denied["details"]["committed_replacements"] == denied["replaced_docs"] == 0
        assert denied["replacement_complete"] is False and denied["replacements"] == []
        assert await stored() == after
