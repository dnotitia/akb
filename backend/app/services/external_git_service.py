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
from app.exceptions import AKBError, MirrorMarkerError
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


class ExternalGitCompatError(AKBError):
    """A FRESH, sterilely re-cloned mirror bare STILL trips the structure
    inspector — a systemic git-version / inspector incompatibility (a false
    positive on our own clean clone), not a sign of tampering.

    Raised to break the otherwise-infinite "re-clone on every poll" loop:
    a sterile clone into a fresh temp dir has no caller-controlled
    pre-existing config, so if the inspector still objects, re-cloning again
    would just repeat forever. The message is value-less (fixed finding
    vocabulary only), so it is safe to log / persist to
    ``vault_external_git.last_error``."""

    def __init__(self, message: str):
        super().__init__(message, status_code=502, code="external_git_compat")


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
    ) -> tuple[str, str]:
        """Make the local bare repo present and trustworthy before reconcile
        reads blobs from it. Returns `(action, materialized_sha)` where action
        is 'cloned', 'fetched', or 'unchanged' and `materialized_sha` is the
        LOCAL `refs/heads/<branch>` SHA after the op. The ls-remote
        `new_sha` is only a change hint; the returned SHA is what actually
        landed on disk, so the caller keys tree/attribution/cursor on it and
        closes the ls-remote↔materialized race.

        Trust is decided by DB sync state + a local integrity probe + a
        structural default-deny check, NOT by on-disk path existence. Any
        untrustworthy local state self-heals via a fresh (inherently sterile)
        clone:

        - No local repo -> clone.
        - A repo exists but is UNTRUSTED -> remove it and clone fresh.
          Untrusted means any of:
            * `last_synced_sha is None` (never recorded a success): a stale
              dir left by a prior same-named vault whose delete cleanup raced
              an in-flight clone, or a clone that crashed before recording
              success;
            * `not git.is_healthy_repo(...)`: a previously-synced repo now
              structurally broken (partial fetch, disk error, kill mid-write);
            * a non-empty structure inspection: a planted
              `remote.*` redirect, `url.*.insteadOf`, `include.*`, credential/
              http/proxy override, hooks, object alternates, replace/grafts,
              or a worktree/commondir marker — we never fetch INTO a suspicious
              repo, we re-clone it fresh instead.
        - A trusted, clean, healthy repo -> 'unchanged' on sha match (SHA read
          from the local ref), else fetch (SHA read from the local ref after
          the tmp-ref promotion).

        The integrity + structure probes inspect only local state, so a
        transient network fetch failure is never mistaken for corruption.
        """
        host = _host_only(remote_url)
        if not self.git.vault_exists(vault_name):
            logger.info("Bootstrap clone: vault=%s host=%s", vault_name, host)
            sha = self.git.clone_mirror(vault_name, remote_url, branch, auth_token)
            return ("cloned", sha)
        # Findings are value-less enum-style codes → safe to log.
        findings = self.git.inspect_mirror_structure(vault_name, remote_url, branch)
        if last_synced_sha is None or findings or not self.git.is_healthy_repo(vault_name):
            if last_synced_sha is None:
                reason = "never-synced"
            elif findings:
                reason = f"structure:{','.join(findings)}"
            else:
                reason = "corrupt"
            logger.warning(
                "Untrusted bare repo for mirror %s (%s); re-cloning from %s",
                vault_name, reason, host,
            )
            self.git.cleanup_vault_dirs(vault_name)
            sha = self.git.clone_mirror(vault_name, remote_url, branch, auth_token)
            # Loop-breaker: a FRESH, sterilely re-cloned bare must be
            # structurally clean. If it STILL trips the structure inspector, the
            # findings are systemic — a git-version / inspector incompatibility
            # (a false positive on our own clean clone), not a sign of tampering
            # (a sterile clone into a fresh temp dir has no caller-controlled
            # pre-existing config). Re-cloning every poll would then
            # loop forever; fail loudly instead. Findings are value-less,
            # so they are safe to surface.
            residual = self.git.inspect_mirror_structure(vault_name, remote_url, branch)
            if residual:
                raise ExternalGitCompatError(
                    f"external-git mirror {vault_name!r}: a fresh re-clone still "
                    f"has structure findings {sorted(residual)} — refusing to "
                    "re-clone in a loop (systemic git/inspector incompatibility)"
                )
            return ("cloned", sha)
        # Self-heal belt: this bare is a trusted, existing mirror we
        # are about to keep (unchanged or fetched — not re-cloned). ensure_local_bare
        # is only ever called for a DB-registered mirror, so if it predates the
        # marker (created before the marker existed, or missed by the startup
        # backfill) stamp it now — otherwise _is_mirror stays False and this
        # mirror's reads keep falling through to GitPython. The unchanged fast
        # path below would otherwise perpetuate the marker-less state forever.
        # Idempotent + cheap (a fast lstat when already marked); the
        # clone/re-clone paths above get their marker from clone_mirror instead.
        self.git.mark_as_mirror(vault_name)
        # Fail-CLOSED: confirm the marker now reads back as a
        # genuine mirror before we KEEP (and serve reads from) this bare. An
        # abnormal marker entry already made mark_as_mirror raise; this also
        # refuses the pathological "mark returned but _is_mirror is still False"
        # case rather than letting the mirror's reads fall open to GitPython.
        if not self.git._is_mirror(vault_name):
            raise MirrorMarkerError(
                f"external-git mirror {vault_name!r} could not establish its "
                "on-disk marker; refusing to serve"
            )
        # Treat the mirror as unchanged ONLY when the ls-remote hint, the
        # recorded cursor, AND the LOCAL materialized ref all agree. If the local
        # ref is missing or differs (e.g. a prior partial reconcile advanced the
        # local ref to B while the cursor stayed at A, and upstream later returned
        # to A), fetch and reconcile to the materialized SHA instead of returning
        # a stale/partial local tree keyed to a different SHA than the cursor.
        try:
            local_sha: str | None = self.git.materialized_sha(vault_name, branch)
        except Exception:  # noqa: BLE001 — missing/unreadable local ref → fetch
            local_sha = None
        if new_sha == last_synced_sha and local_sha == last_synced_sha:
            return ("unchanged", local_sha)
        sha = self.git.fetch_remote(vault_name, remote_url, branch, auth_token)
        return ("fetched", sha)

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

        # Cheap network check first. This SHA is only a change-detection HINT:
        # everything downstream keys on the LOCAL materialized SHA
        # returned by ensure_local_bare, never on this value.
        new_sha = await asyncio.to_thread(
            self.git.ls_remote_head,
            cfg["remote_url"], cfg["remote_branch"], cfg["auth_token"],
        )
        if new_sha is None:
            # Redacted: never echo the remote URL into logs / last_error.
            raise RuntimeError(
                f"Remote branch '{cfg['remote_branch']}' not found on the mirror upstream"
            )

        # Ensure a trusted local bare repo exists. The clone-vs-fetch decision
        # keys on DB sync state (last_synced_sha) + integrity/structure probes,
        # not on-disk path existence — see ensure_local_bare. It returns the
        # authoritative materialized SHA (local ref), which we use from here on
        # so a force-push between ls-remote and fetch can't desync the tree
        # from the cursor. 'unchanged' short-circuits the rest.
        action, materialized_sha = await asyncio.to_thread(
            self.ensure_local_bare,
            vault_name,
            cfg["last_synced_sha"],
            new_sha,
            cfg["remote_url"],
            cfg["remote_branch"],
            cfg["auth_token"],
        )
        if action == "unchanged":
            marked = await ext_repo.mark_success(
                vault_id, cfg["poll_interval_secs"],
                validated_url=cfg["remote_url"], validated_token=cfg["auth_token"],
            )
            if not marked:
                # The row was quarantined OR reconfigured out from under this
                # reconcile: the snapshot-CAS (sync_state='active' AND the fetched
                # remote_url/auth_token) matched zero rows. Report superseded so the
                # caller does not count it as processed work; the terminal / new
                # config is left untouched.
                logger.info(
                    "External sync superseded (row no longer active): vault=%s",
                    vault_name,
                )
                return {"status": "superseded", "sha": materialized_sha}
            return {"status": "unchanged", "sha": materialized_sha}

        remote_tree = await asyncio.to_thread(self.git.ls_tree, vault_name, materialized_sha)

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
                # Oversized-blob gate: size the blob with
                # `cat-file -s` BEFORE materializing it. An oversized blob is a
                # DETERMINISTIC skip — re-polling cannot shrink it — so it counts
                # as `skipped`, NEVER `errors`: errors hold the cursor (the
                # RuntimeError below), which would wedge the whole poll forever on
                # a single big file. The cap lives in git_service so this module
                # stays free of a `settings` dependency (backfill-unconditional).
                blob_bytes, oversized = await asyncio.to_thread(
                    self.git.blob_exceeds_max, vault_name, blob_sha
                )
                if oversized:
                    if existing:
                        # The path was small + indexed and upstream grew it past
                        # the cap. A plain skip would leave the prior (smaller)
                        # content exposed in the index/DB — tombstone it so
                        # nothing stale is served. If the tombstone
                        # itself fails it raises → errors++ (cursor held), so the
                        # stale content is never left behind on a silent failure.
                        # ``expected_blob`` is the prior (small) blob our snapshot
                        # saw, so a concurrent re-index is not clobbered.
                        outcome = await self._delete_external_path(
                            vault_id=vault_id, vault_name=vault_name, path=path,
                            expected_blob=existing["external_blob"],
                        )
                        if outcome == "conflict":
                            # A concurrent reconcile moved this path's blob after
                            # our snapshot; the tombstone CAS did not match. Treat
                            # as retryable (errors++) so the cursor is HELD and the
                            # next poll reprocesses from truth — NEVER counted as a
                            # clean skip that would let mark_success advance the
                            # cursor over stale content.
                            errors += 1
                            logger.warning(
                                "External oversized tombstone conflict (blob "
                                "moved); holding cursor: vault=%s path=%s",
                                vault_name, path,
                            )
                            continue
                        logger.info(
                            "External oversized blob tombstoned prior content: "
                            "vault=%s path=%s size=%d",
                            vault_name, path, blob_bytes,
                        )
                    else:
                        # A brand-new oversized path is simply never indexed.
                        logger.info(
                            "External oversized blob skipped: vault=%s path=%s size=%d",
                            vault_name, path, blob_bytes,
                        )
                    skipped += 1
                    continue
                await self._reindex_file(
                    vault_id=vault_id, vault_name=vault_name,
                    path=path, blob_sha=blob_sha, remote_url=cfg["remote_url"],
                    tip_sha=materialized_sha,
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
                outcome = await self._delete_external_path(
                    vault_id=vault_id, vault_name=vault_name, path=path,
                    expected_blob=local[path]["external_blob"],
                )
                if outcome == "conflict":
                    # A concurrent reconcile re-indexed this path to a newer blob
                    # after our snapshot, so it is NOT actually gone from truth.
                    # Hold the cursor (errors++) rather than counting a delete that
                    # would advance past a live document.
                    errors += 1
                    logger.warning(
                        "External delete conflict (blob moved); holding cursor: "
                        "vault=%s path=%s",
                        vault_name, path,
                    )
                    continue
                deleted += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                logger.warning(
                    "External delete failed: vault=%s path=%s err=%s",
                    vault_name, path, e,
                )

        result = {
            "status": "synced", "sha": materialized_sha,
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
        marked = await ext_repo.mark_success(
            vault_id, cfg["poll_interval_secs"], new_sha=materialized_sha,
            validated_url=cfg["remote_url"], validated_token=cfg["auth_token"],
        )
        if not marked:
            # Superseded (quarantined OR reconfigured mid-reconcile): the
            # snapshot-CAS (sync_state='active' AND the fetched remote_url/auth_token)
            # matched zero rows. Report it so the caller does not count success; the
            # terminal / new config is left untouched.
            # The per-file writes already applied are harmless and idempotent — the
            # cursor simply was not advanced.
            logger.info(
                "External sync superseded (row no longer active): vault=%s", vault_name,
            )
            result["status"] = "superseded"
            return result
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
        # saved NFD would otherwise corrupt the BM25 + embedding index.
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
            # could otherwise let them slip in via upstream directory names,
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
                    vault_name=vault_name,
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
        self,
        *,
        vault_id: uuid.UUID,
        vault_name: str,
        path: str,
        expected_blob: str | None = None,
    ) -> str:
        """Tombstone an external_git document (path removed upstream, or grown
        past the oversized cap), race-safely. Returns the outcome so the
        caller can hold the cursor on a CAS conflict:

        * ``"deleted"`` — the row was removed and the count/event fired once;
        * ``"already_absent"`` — no matching row (a concurrent reconcile already
          tombstoned it); a clean no-op;
        * ``"conflict"`` — ``expected_blob`` did NOT match the row's current blob,
          so a concurrent reconcile re-indexed this path AFTER our snapshot. The
          row is LEFT in place, and the caller MUST treat this as retryable — NOT
          as a successful skip/delete — so ``mark_success`` does not advance the
          cursor over content this reconcile never reconciled. Otherwise the next
          poll's ``unchanged`` fast-path (cursor == upstream SHA) would perpetuate
          the stale prior content forever.

        The row is re-read and LOCKED (``FOR UPDATE``) INSIDE the transaction,
        never before it. Two reconciles that overlap (a claim lease that expired
        and let a second run start) would otherwise both read the row pre-tx,
        both ``DELETE`` (the second matching zero rows) yet BOTH decrement the
        collection count and emit a duplicate ``document.delete`` event. The lock
        serializes them; the loser sees no row and returns without touching the
        count or events. ``expected_blob`` (the blob the caller's snapshot saw)
        further guards against deleting a version a concurrent reconcile already
        re-indexed. The collection decrement and the event fire ONLY when
        ``DELETE … RETURNING`` actually removed a row.
        """
        pool = await get_pool()
        coll_repo = CollectionRepository(pool)
        doc_repo = DocumentRepository(pool)
        from app.services.kg_service import delete_document_relations
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id, collection_id, created_by, external_blob
                      FROM documents
                     WHERE vault_id = $1 AND source = 'external_git'
                           AND external_path = $2
                     FOR UPDATE
                    """,
                    vault_id, path,
                )
                if row is None:
                    return "already_absent"  # tombstoned by a concurrent reconcile
                if expected_blob is not None and row["external_blob"] != expected_blob:
                    # A concurrent reconcile re-indexed this path to a newer blob
                    # after our snapshot; deleting now would drop the fresher
                    # version. Leave it and report the conflict so the caller holds
                    # the cursor — the next poll reconciles from truth.
                    return "conflict"
                await delete_document_chunks(conn, str(row["id"]))
                # Remove implicit edges anchored on this doc — otherwise
                # they survive the delete and dangle on the next BFS.
                await delete_document_relations(conn, vault_name, path)
                # The row delete and the publication cascade are one call
                # through the repository chokepoint. Migration 058's FK only
                # cascades rows that carry a `document_id`, which the
                # backfill could not give every legacy row, so the tombstone
                # still has to drop the publication itself, on THIS
                # connection: without it,
                # an upstream commit that removes a mirrored path leaves a
                # slug that still resolves by path — and a later upstream
                # commit re-adding that path (a revert, a rename back)
                # republishes whatever now lives there under the old public
                # slug. The chokepoint derives that URI from `documents.path`
                # under its own lock — `documents.path == external_path` for
                # mirrored docs (see `upsert_external`), so it is the same URI
                # `emit_event` reports below, but read from the row rather
                # than from this function's `path` argument.
                #
                # The chokepoint re-takes `FOR UPDATE` on the row we already
                # locked above; on the same transaction that is a no-op, and
                # keeping it there rather than relying on this caller is the
                # point — the lock is what closes the publish/delete race, and
                # it must not depend on every caller remembering.
                deleted = await doc_repo.delete_with_publications(
                    conn, doc_id=row["id"], vault_id=vault_id,
                )
                if not deleted:
                    # Unreachable while we hold the row lock (no other tx can
                    # delete it first), but keep the count/event STRICTLY tied to
                    # a real row deletion rather than a stale pre-read. Nothing
                    # changed, so this is a no-op (not a CAS conflict).
                    return "already_absent"
                if row["collection_id"]:
                    await coll_repo.decrement_count(
                        row["collection_id"],
                        datetime.now(timezone.utc),
                        conn=conn,
                    )
                await emit_event(
                    conn,
                    "document.delete",
                    vault_id=vault_id,
                    resource_uri=doc_uri(vault_name, path),
                    actor_id=row["created_by"],
                    payload={"path": path, "source": "external_git"},
                )
        return "deleted"


# ── Startup marker backfill ─────────────────────


async def backfill_mirror_markers(git: GitService | None = None) -> int:
    """Stamp the on-disk external-mirror marker on every mirror the DB knows
    about whose bare repo predates it.

    ``vault_external_git`` is the authoritative record of which vaults are
    mirrors. The marker (``git_service._MIRROR_MARKER``) is what routes a vault's
    reads through the hermetic runner (``GitService._is_mirror``); mirrors
    created before the marker existed carry none, so their reads would fall
    through to GitPython — the fail-open gap this closes. This enumerates the
    DB's mirrors and marks any whose bare exists but is unmarked.

    Called from the app lifespan AFTER the DB is up (so the mirror list is
    authoritative) and BEFORE the poller starts / requests are served, so no
    reader path or reconcile can observe an unmarked mirror. Idempotent — safe
    to run on every startup. Returns the number of markers newly written.

    FAIL-FAST (serving must not begin until the marker is guaranteed on
    every EXISTING mirror bare): if a
    marker cannot be WRITTEN onto an existing bare (a real disk/permission
    fault), this raises so the lifespan aborts boot BEFORE serving — zero
    fail-open window. A mirror whose bare is simply absent (not yet cloned; the
    poller's ``clone_mirror`` writes the marker on first clone) is a normal skip,
    NOT a failure.

    UNCONDITIONAL (kill-switch consistency): this runs even when
    ``external_git_enabled`` is off. The marker is the fail-CLOSED safety net
    that makes the read paths correctly REFUSE a disabled mirror (a 503) instead
    of silently serving it through GitPython; a partial kill-switch that skipped
    the backfill would leave marker-less mirrors reading fail-OPEN. The
    kill-switch lives on the poller-start gate and the read paths, NOT here.
    """
    git = git or GitService()
    pool = await get_pool()
    # A DB-read failure here is a total failure (the mirror list is
    # unknowable) — let it propagate so the lifespan aborts boot rather than
    # serving with an unverified mirror set.
    names = await VaultExternalGitRepository(pool).list_mirror_vault_names()
    if not names:
        return 0
    # Marker writes are blocking fs ops; run the sweep off the event loop.
    marked, failed = await asyncio.to_thread(_stamp_mirror_markers, git, names)
    if failed:
        # Only vault NAMES (secret-free) in the message. Boot aborts here, in the
        # lifespan before start_workers/serving, so no request or poll can observe
        # an unmarked mirror.
        raise RuntimeError(
            f"external-git mirror marker backfill failed for {len(failed)} "
            f"vault(s): {', '.join(sorted(failed))}. Refusing to start — these "
            "existing mirrors would fall open to GitPython on read. Resolve the "
            "underlying disk/permission fault and restart."
        )
    return marked


def _stamp_mirror_markers(git: GitService, vault_names: list[str]) -> tuple[int, list[str]]:
    """Stamp the mirror marker for each named mirror (design: DB-authoritative).

    Attempts EVERY vault, collecting the names whose marker WRITE failed (a real
    disk/permission fault on an EXISTING bare) so the caller can fail-fast on the
    whole set rather than aborting on the first fault. A vault whose bare is
    simply absent — not yet cloned; the poller's ``clone_mirror`` writes the
    marker on first clone — is a normal skip (``mark_as_mirror`` returns False
    without raising), NOT a failure. Returns
    ``(markers_newly_written, failed_vault_names)``.

    Split out from ``backfill_mirror_markers`` so the sweep logic is unit-testable
    without a live DB (the DB query is the only untested seam)."""
    marked = 0
    failed: list[str] = []
    for name in vault_names:
        try:
            if git.mark_as_mirror(name):
                marked += 1
        except Exception:  # noqa: BLE001 — collect; the caller fail-fasts on the set
            logger.warning("Mirror marker backfill failed: vault=%s", name, exc_info=True)
            failed.append(name)
    return marked, failed


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
