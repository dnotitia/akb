from __future__ import annotations

import uuid
from hashlib import sha256

import pytest

from app.services import resource_integrity


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AcquireContext(self.conn)


class _Conn:
    def __init__(self, *, document_rows: list[dict], file_rows: list[dict]):
        self.document_rows = document_rows
        self.file_rows = file_rows
        self.executions: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        if "FROM documents" in sql:
            return self.document_rows
        if "FROM vault_files" in sql:
            return self.file_rows
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def execute(self, sql: str, *args):
        self.executions.append((sql, args))


class _Git:
    def __init__(self, raw_by_path: dict[tuple[str, str], str]):
        self.raw_by_path = raw_by_path
        self.reads: list[tuple[str, str, str | None]] = []

    def read_file(self, vault: str, path: str, commit: str | None = None):
        self.reads.append((vault, path, commit))
        return self.raw_by_path[(vault, path)]


class _S3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.heads: list[str] = []
        self.reads: list[str] = []

    def head(self, key: str):
        self.heads.append(key)
        return {
            "ContentLength": len(self.objects[key]),
            "ETag": '"etag-for-test"',
            "VersionId": "version-for-test",
        }

    def iter_chunks(self, key: str):
        self.reads.append(key)
        yield self.objects[key]


def test_document_hash_projection_uses_markdown_body_not_frontmatter() -> None:
    raw = "---\ntitle: Ignored\n---\n# Body\n\nreal content\n"

    projection = resource_integrity.document_hash_projection(raw, "abc123")

    assert projection == {
        "content_hash": sha256("# Body\n\nreal content".encode("utf-8")).hexdigest(),
        "hash_algorithm": "sha256",
        "content_hash_commit": "abc123",
    }


def test_file_hash_projection_uses_s3_bytes_and_confirmed_metadata() -> None:
    payload = b"file bytes from storage"

    projection = resource_integrity.file_hash_projection(
        chunks=[payload],
        head={"ContentLength": len(payload), "ETag": '"abc"', "VersionId": "v1"},
    )

    assert projection == {
        "size_bytes": len(payload),
        "content_hash": sha256(payload).hexdigest(),
        "hash_algorithm": "sha256",
        "etag": "abc",
        "storage_version": "v1",
    }


@pytest.mark.asyncio
async def test_repair_resource_hashes_backfills_documents_and_files(monkeypatch) -> None:
    doc_id = uuid.uuid4()
    file_id = uuid.uuid4()
    conn = _Conn(
        document_rows=[
            {
                "id": doc_id,
                "vault_name": "repair-vault",
                "path": "notes/doc.md",
                "current_commit": "c" * 40,
            },
        ],
        file_rows=[
            {
                "id": file_id,
                "vault_name": "repair-vault",
                "s3_key": "repair-vault/files/blob.bin",
            },
        ],
    )
    git = _Git({
        ("repair-vault", "notes/doc.md"): "---\ntitle: Doc\n---\nBody from git\n",
    })
    s3 = _S3({"repair-vault/files/blob.bin": b"bytes from s3"})
    file_updates: list[tuple] = []

    async def fake_get_pool():
        return _Pool(conn)

    async def fake_update_confirmed_metadata(conn_arg, row_id, **metadata):
        file_updates.append((conn_arg, row_id, metadata))

    monkeypatch.setattr(resource_integrity, "get_pool", fake_get_pool)
    monkeypatch.setattr(resource_integrity.file_service, "s3_adapter", s3)
    monkeypatch.setattr(
        resource_integrity.vault_files_repo,
        "update_confirmed_metadata",
        fake_update_confirmed_metadata,
    )

    report = await resource_integrity.repair_resource_hashes(
        vault="repair-vault",
        limit=10,
        git=git,
    )

    assert report == {
        "documents_checked": 1,
        "documents_repaired": 1,
        "files_checked": 1,
        "files_repaired": 1,
        "errors": [],
    }
    assert git.reads == [("repair-vault", "notes/doc.md", "c" * 40)]
    assert s3.heads == ["repair-vault/files/blob.bin"]
    assert s3.reads == ["repair-vault/files/blob.bin"]

    _, doc_args = conn.executions[0]
    assert doc_args == (
        sha256("Body from git".encode("utf-8")).hexdigest(),
        "sha256",
        "c" * 40,
        doc_id,
    )
    assert file_updates == [
        (
            conn,
            file_id,
            {
                "size_bytes": len(b"bytes from s3"),
                "content_hash": sha256(b"bytes from s3").hexdigest(),
                "hash_algorithm": "sha256",
                "etag": "etag-for-test",
                "storage_version": "version-for-test",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_repair_resource_hashes_reports_per_resource_errors(monkeypatch) -> None:
    conn = _Conn(
        document_rows=[
            {
                "id": uuid.uuid4(),
                "vault_name": "repair-vault",
                "path": "missing.md",
                "current_commit": "d" * 40,
            },
        ],
        file_rows=[],
    )

    class BrokenGit:
        def read_file(self, vault: str, path: str, commit: str | None = None):
            raise FileNotFoundError(path)

    async def fake_get_pool():
        return _Pool(conn)

    monkeypatch.setattr(resource_integrity, "get_pool", fake_get_pool)

    report = await resource_integrity.repair_resource_hashes(
        include_files=False,
        git=BrokenGit(),
    )

    assert report["documents_checked"] == 1
    assert report["documents_repaired"] == 0
    assert report["files_checked"] == 0
    assert report["errors"] == ["document repair-vault/missing.md: missing.md"]
