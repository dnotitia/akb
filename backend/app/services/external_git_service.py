"""External-git read-only mirror — tree-sha reconciliation.

A vault registered in `vault_external_git` is kept in sync with an
upstream git repo by comparing the upstream tree (every file's blob sha)
against `documents.external_blob` for that vault. The poller is the only
caller; users see these vaults as read-only via the access guard in
`access_service.check_vault_access`.

Design notes:
- No diff parsing. Status codes (A/M/D/R) collapse into "blob shas
  changed" vs "path disappeared", which the reconciler handles
  uniformly. This stays correct under non-linear upstream history
  (force-push, rebase) where diff-from-old-sha would break.
- The reconciler is idempotent. Crashing mid-sync leaves the cursor
  unchanged; the next poll redoes the same work and converges.
- Embeddings are NOT generated inline. New chunks land with NULL
  embedding, and `embed_worker` + `delete_worker` carry them the rest
  of the way. This keeps sync time bounded by git I/O, not by the
  embedding API's mood.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

import frontmatter

from urllib.parse import urlsplit

from app.db.postgres import get_pool
from app.repositories.document_repo import CollectionRepository, DocumentRepository
from app.repositories.events_repo import emit_event
from app.repositories.vault_external_git_repo import VaultExternalGitRepository
from app.services.git_service import GitService
from app.services.index_service import (
    build_doc_metadata_header,
    chunk_markdown,
    delete_document_chunks,
    write_source_chunks,
)
from app.services.resource_hash import HASH_ALGORITHM, compute_text_content_hash
from app.services.uri_service import doc_uri
from app.util.text import normalize_collection_path, to_nfc, to_nfc_any

logger = logging.getLogger("akb.external_git")


# Files we ingest as text documents. Anything else is silently skipped
# for MVP — when we want to mirror PDFs / images / source code, route
# them to file_service / table_service instead from `_classify`.
_TEXT_DOC_SUFFIXES = (".md", ".markdown", ".mdx", ".txt", ".rst", ".adoc")
_FRONTMATTER_SUFFIXES = (".md", ".markdown", ".mdx")

_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def _host_only(url: str) -> str:
    """Redact userinfo before logging. Callers may pass
    `https://token@host/...` forms; the hostname is the only part we
    want to surface in operational logs."""
    try:
        return urlsplit(url).hostname or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


class ExternalGitService:
    """Encapsulates clone/fetch/reconcile for read-only mirror vaults."""

    def __init__(self, git: GitService | None = None):
        self.git = git or GitService()

    # ── Bootstrap (local bare repo) ──────────────────────────

    def ensure_local_bare(
        self,
        vault_name: str,
        last_synced_sha: str | None,
        new_sha: str,
        remote_url: str,
        branch: str,
        auth_token: str | None,
    ) -> str:
        """Make the local bare repo present and trustworthy before
        reconcile reads blobs from it. Returns 'cloned', 'fetched', or
        'unchanged'.

        Trust is decided by DB sync state + a local integrity probe, NOT
        by on-disk path existence. Any untrustworthy local state
        self-heals via a fresh clone:

        - No local repo -> clone.
        - A repo exists but is UNTRUSTED -> remove it and clone fresh.
          Untrusted means either:
            * `last_synced_sha is None` (never recorded a success): a stale
              dir left by a prior same-named vault whose delete cleanup
              raced an in-flight clone, or a clone that crashed before
              recording success; or
            * `not git.is_healthy_repo(...)`: a previously-synced repo that
              is now structurally broken (partial fetch, disk error, kill
              mid-write).
          reconcile is idempotent and the first success sets
          `last_synced_sha`, so a stale-dir re-clone happens at most once;
          a corruption re-clone only fires while the repo is actually
          broken.
        - A trusted, healthy repo -> 'unchanged' on sha match, else fetch.

        Keying on `vault_exists()` alone (the previous behaviour) silently
        adopted a stale/corrupt dir: the clone was skipped and the
        subsequent fetch ran against a broken repo, failing on every
        retry. The companion serialization of `cleanup_vault_dirs` under
        `_vault_lock` closes the race that *creates* the stale dir; this
        method closes its *adoption* (and corruption from any other
        cause). The integrity probe inspects only local state, so a
        transient network fetch failure is never mistaken for corruption.
        """
        host = _host_only(remote_url)
        if not self.git.vault_exists(vault_name):
            logger.info("Bootstrap clone: vault=%s host=%s", vault_name, host)
            self.git.clone_mirror(vault_name, remote_url, branch, auth_token)
            return "cloned"
        if last_synced_sha is None or not self.git.is_healthy_repo(vault_name):
            reason = "never-synced" if last_synced_sha is None else "corrupt"
            logger.warning(
                "Untrusted bare repo for mirror %s (%s); re-cloning from %s",
                vault_name, reason, host,
            )
            self.git.cleanup_vault_dirs(vault_name)
            self.git.clone_mirror(vault_name, remote_url, branch, auth_token)
            return "cloned"
        if new_sha == last_synced_sha:
            return "unchanged"
        self.git.fetch_remote(vault_name, remote_url, branch, auth_token)
        return "fetched"

    # ── Reconcile (called by poller) ─────────────────────────

    async def reconcile(self, vault_id: uuid.UUID, vault_name: str) -> dict:
        """Bring `documents` for this vault into sync with the upstream
        tree. Returns a dict of counters for logging/metrics.

        On first poll for a freshly-created mirror, the local bare repo
        doesn't exist yet — the poller is where we do the initial clone.
        Keeping the heavy network I/O in the worker path (not the MCP
        request path) means vault creation stays snappy and a server
        restart mid-bootstrap is harmless: the worker retries on the
        next poll.
        """
        pool = await get_pool()
        ext_repo = VaultExternalGitRepository(pool)
        cfg = await ext_repo.get(vault_id)
        if cfg is None:
            raise ValueError(f"vault_external_git missing for {vault_name}")

        # Cheap network check first.
        new_sha = await asyncio.to_thread(
            self.git.ls_remote_head,
            cfg["remote_url"], cfg["remote_branch"], cfg["auth_token"],
        )
        if new_sha is None:
            raise RuntimeError(
                f"Remote branch '{cfg['remote_branch']}' not found at {cfg['remote_url']}"
            )

        # Ensure a trusted local bare repo exists. The clone-vs-fetch
        # decision keys on DB sync state (last_synced_sha), not on-disk
        # path existence — see ensure_local_bare. 'unchanged' short-
        # circuits the rest of the reconcile.
        action = await asyncio.to_thread(
            self.ensure_local_bare,
            vault_name,
            cfg["last_synced_sha"],
            new_sha,
            cfg["remote_url"],
            cfg["remote_branch"],
            cfg["auth_token"],
        )
        if action == "unchanged":
            await ext_repo.mark_success(vault_id, cfg["poll_interval_secs"])
            return {"status": "unchanged", "sha": new_sha}

        remote_tree = await asyncio.to_thread(self.git.ls_tree, vault_name, new_sha)

        doc_repo = DocumentRepository(pool)
        local = await doc_repo.list_external_blobs(vault_id)

        added, updated, deleted, skipped, errors = 0, 0, 0, 0, 0

        for path, blob_sha in remote_tree.items():
            if not _is_indexable(path):
                skipped += 1
                continue
            existing = local.get(path)
            if existing and existing["external_blob"] == blob_sha:
                continue  # unchanged
            try:
                await self._reindex_file(
                    vault_id=vault_id, vault_name=vault_name,
                    path=path, blob_sha=blob_sha, remote_url=cfg["remote_url"],
                    tip_sha=new_sha,
                )
                if existing:
                    updated += 1
                else:
                    added += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                logger.warning(
                    "Reindex failed: vault=%s path=%s blob=%s err=%s",
                    vault_name, path, blob_sha, e,
                )

        for path in local.keys() - remote_tree.keys():
            try:
                await self._delete_external_path(
                    vault_id=vault_id, vault_name=vault_name, path=path
                )
                deleted += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                logger.warning(
                    "External delete failed: vault=%s path=%s err=%s",
                    vault_name, path, e,
                )

        result = {
            "status": "synced", "sha": new_sha,
            "added": added, "updated": updated, "deleted": deleted,
            "skipped": skipped, "errors": errors,
        }
        if errors:
            # Don't advance the cursor while some files are still failing —
            # otherwise the next poll takes the `unchanged` fast path and
            # we never retry. The poller's own mark_failure will set a
            # backoff interval; do not overwrite it here.
            result["status"] = "partial"
            logger.warning(
                "External sync partial: vault=%s errors=%d (cursor not advanced)",
                vault_name, errors,
            )
            raise RuntimeError(
                f"{errors} file(s) failed to reindex; cursor held at "
                f"{cfg['last_synced_sha']}"
            )
        await ext_repo.mark_success(vault_id, cfg["poll_interval_secs"], new_sha=new_sha)
        logger.info("External sync complete: vault=%s %s", vault_name, result)
        return result

    # ── Per-file ─────────────────────────────────────────────

    async def _reindex_file(
        self,
        *,
        vault_id: uuid.UUID,
        vault_name: str,
        path: str,
        blob_sha: str,
        remote_url: str,
        tip_sha: str,
    ) -> None:
        raw = await asyncio.to_thread(self.git.cat_blob, vault_name, blob_sha)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Treat undecodable text as a skip — caller logs.
            raise ValueError(f"non-utf8 content at {path}")

        # Normalize upstream text to NFC. Git usually stores NFC-encoded
        # Korean already, but an upstream committer on macOS whose editor
        # saved NFD would otherwise poison the BM25 + embedding index.
        path = to_nfc(path)
        content = to_nfc(content)

        fm_dict, body = _split_frontmatter(path, content)
        fm_dict = to_nfc_any(fm_dict)
        title = _derive_title(fm_dict, body, path)
        tags = _coerce_tags(fm_dict.get("tags"))
        summary = fm_dict.get("summary")
        domain = fm_dict.get("domain")
        doc_type = fm_dict.get("type")

        # No short `id` field — idempotency across re-syncs is already
        # guaranteed by `documents UNIQUE(vault_id, path)`, and the
        # canonical handle is the akb:// URI built from (vault, path).
        # `external_path` keeps the upstream-side path so subscribers
        # can map back to the source repo.
        metadata = {**{k: v for k, v in fm_dict.items() if k not in {
            "title", "type", "tags", "summary", "domain", "source",
        }}, "external_path": path}

        # Per-file last-touch commit — keeps `documents.current_commit`
        # meaningful across multiple syncs. Cheap compared to the cat-blob
        # / chunking work we're already doing for this path.
        last_commit = await asyncio.to_thread(
            self.git.last_commit_for_path, vault_name, path, tip_sha
        )
        created_by = _created_by_for(remote_url)
        now = datetime.now(timezone.utc)

        parent = str(PurePosixPath(path).parent)
        raw_coll = "" if parent in (".", "") else parent
        try:
            # Same validator the user-write path uses (rejects reserved
            # segments `coll`/`doc`/`table`/`file`). External mirrors
            # otherwise smuggle them in via upstream directory names,
            # and the corresponding `akb://` URIs would be unparseable.
            coll_path = normalize_collection_path(raw_coll, allow_empty=True)
        except ValueError as e:
            raise ValueError(
                f"external_git path {path!r} maps to invalid collection "
                f"{raw_coll!r}: {e}"
            )

        meta_header = build_doc_metadata_header(
            vault_name=vault_name, path=path, title=title,
            summary=summary, tags=tags, doc_type=doc_type,
        )
        chunks = chunk_markdown(body, metadata_header=meta_header)

        # One connection, one tx: collection get-or-create → doc upsert →
        # chunks replace. Halves the pool acquires per file (5658 ×).
        pool = await get_pool()
        doc_repo = DocumentRepository(pool)
        coll_repo = CollectionRepository(pool)
        async with pool.acquire() as conn:
            async with conn.transaction():
                collection_id = (
                    await coll_repo.get_or_create(vault_id, coll_path, conn=conn)
                    if coll_path else None
                )
                pg_doc_id, inserted = await doc_repo.upsert_external(
                    vault_id=vault_id,
                    collection_id=collection_id,
                    path=path,
                    external_path=path,
                    external_blob=blob_sha,
                    title=title,
                    doc_type=doc_type,
                    summary=summary,
                    domain=domain,
                    tags=tags,
                    metadata=metadata,
                    now=now,
                    commit_hash=last_commit,
                    content_hash=compute_text_content_hash(body),
                    hash_algorithm=HASH_ALGORITHM,
                    created_by=created_by,
                    conn=conn,
                )
                if inserted and collection_id is not None:
                    await coll_repo.increment_count(collection_id, now, conn=conn)
                # Empty embeddings -> chunks land with NULL embedding
                # column; embed_worker fills + upserts; delete_worker ships
                # to the vector store. write_source_chunks drops + inserts, so this
                # handles both fresh inserts and re-chunking.
                await write_source_chunks(
                    conn, "document", str(pg_doc_id),
                    vault_id=vault_id,
                    chunks=chunks,
                )
                # Subscribers (search reindex, audit) need to see external
                # mirror writes the same as user PUTs. Emitted inside the
                # same TX so rollback drops the event too.
                await emit_event(
                    conn,
                    "document.put" if inserted else "document.update",
                    vault_id=vault_id,
                    resource_uri=doc_uri(vault_name, path),
                    actor_id=created_by,
                    payload={
                        "path": path,
                        "title": title,
                        "doc_type": doc_type,
                        "external_blob": blob_sha,
                        "commit": last_commit,
                        "content_hash": compute_text_content_hash(body),
                        "hash_algorithm": HASH_ALGORITHM,
                    },
                )

    async def _delete_external_path(
        self, *, vault_id: uuid.UUID, vault_name: str, path: str
    ) -> None:
        pool = await get_pool()
        doc_repo = DocumentRepository(pool)
        coll_repo = CollectionRepository(pool)
        existing = await doc_repo.find_by_external_path(vault_id, path)
        if not existing:
            return
        from app.services.kg_service import delete_document_relations
        async with pool.acquire() as conn:
            async with conn.transaction():
                await delete_document_chunks(conn, str(existing["id"]))
                # Remove implicit edges anchored on this doc — otherwise
                # they survive the delete and dangle on the next BFS.
                await delete_document_relations(conn, vault_name, path)
                await conn.execute("DELETE FROM documents WHERE id = $1", existing["id"])
                if existing.get("collection_id"):
                    await coll_repo.decrement_count(
                        existing["collection_id"],
                        datetime.now(timezone.utc),
                        conn=conn,
                    )
                await emit_event(
                    conn,
                    "document.delete",
                    vault_id=vault_id,
                    resource_uri=doc_uri(vault_name, path),
                    actor_id=existing.get("created_by"),
                    payload={"path": path, "source": "external_git"},
                )


# ── Helpers ──────────────────────────────────────────────────


def _is_indexable(path: str) -> bool:
    """Skip dotfiles/dotdirs and anything that isn't text-shaped enough
    for chunk_markdown to do something useful with. Conservative for
    MVP — extend the suffix list (or branch into table/file routing)
    when we need to mirror richer content."""
    p = PurePosixPath(path)
    if any(part.startswith(".") for part in p.parts):
        return False
    return p.suffix.lower() in _TEXT_DOC_SUFFIXES


def _split_frontmatter(path: str, content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter only for markdown-family files; plain
    text/rst goes through as-is so a `---` divider in those formats
    isn't misread as frontmatter delimiters."""
    if PurePosixPath(path).suffix.lower() not in _FRONTMATTER_SUFFIXES:
        return {}, content
    try:
        post = frontmatter.loads(content)
        return dict(post.metadata), post.content
    except Exception:  # noqa: BLE001
        return {}, content


def _derive_title(fm_dict: dict, body: str, path: str) -> str:
    raw = fm_dict.get("title")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    m = _H1_RE.search(body)
    if m:
        return m.group(1).strip()
    return PurePosixPath(path).stem or path


def _created_by_for(remote_url: str) -> str:
    """Audit trail stamp for external_git docs. We don't know which
    human authored the upstream commit (multiple potentially), so we
    record the source host — useful for filtering / search and to
    distinguish manually-put docs in UI."""
    host = urlsplit(remote_url).hostname or "unknown"
    return f"external_git:{host}"


def _coerce_tags(value) -> list[str]:
    """Frontmatter `tags` can show up as a list, a comma-separated
    string, or a single string — normalize to list[str] for the DB
    column."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return [str(value)]
