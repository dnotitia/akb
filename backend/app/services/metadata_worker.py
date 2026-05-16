"""Background worker that fills LLM-derived metadata on imported docs.

Scope: only documents with `source = 'external_git'` and
`llm_metadata_at IS NULL`. Manual documents already have author-provided
summary/tags and are intentionally untouched.

Pipeline per doc:

1. Render the doc body and call the configured LLM with a strict JSON
   schema asking for {summary, tags, doc_type, domain}.
2. Apply via `DocumentRepository.mark_llm_metadata_filled`, which only
   writes into NULL/empty fields so frontmatter-provided values always
   win.

Loop / backoff / abandonment use the shared `BackfillRunner` —
identical contract to `embed_worker`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid  # noqa: F401  — referenced in PEP 563 string annotation
from datetime import datetime, timedelta, timezone

from app.db.postgres import get_pool
from app.repositories.document_repo import DocumentRepository
from app.services._backfill import BackfillRunner, MAX_RETRIES, next_attempt_delay
from app.services.git_service import GitService
from app.services.llm_service import LLMError, LLMPermanentError, chat_json
from app.util.text import to_nfc_any

logger = logging.getLogger("akb.metadata_worker")

BATCH_SIZE = 4
MAX_BODY_CHARS = 6000  # truncate very long docs before sending to the LLM

_DOC_TYPES = {"note", "report", "decision", "spec", "plan", "session", "task", "reference", "skill"}

_SYSTEM_PROMPT = (
    "You are a metadata extractor for a technical knowledge base. "
    "Given a document, return a JSON object with these fields:\n"
    "  - summary:  one or two sentences summarising the document.\n"
    "  - tags:     5-10 short kebab-case topical tags.\n"
    "  - doc_type: exactly one of "
    f"{sorted(_DOC_TYPES)}, choose the closest fit.\n"
    "  - domain:   a short noun phrase naming the subject area "
    "(e.g. 'engineering', 'legal', 'product', 'finance').\n"
    "Respond with JSON only — no prose, no code fences."
)


async def _claim_batch(conn) -> list[dict]:
    """Claim up to BATCH_SIZE pending external docs + fetch their vault
    name in one shot (avoids an N+1 vault lookup in the process loop).
    The lookahead interval comes from settings so a large mirror doing
    a slow LLM batch doesn't get its rows re-claimed by a peer worker.
    """
    from app.config import settings  # local import to dodge circular import at module load
    rows = await conn.fetch(
        """
        WITH pending AS (
            SELECT id
              FROM documents
             WHERE source = 'external_git'
               AND llm_metadata_at IS NULL
               AND (llm_next_attempt_at IS NULL OR llm_next_attempt_at <= NOW())
               AND llm_retry_count < $2
             ORDER BY llm_next_attempt_at NULLS FIRST, id
             LIMIT $1
             FOR UPDATE SKIP LOCKED
        )
        UPDATE documents d
           SET llm_next_attempt_at = NOW() + ($3 || ' seconds')::interval
          FROM pending p
         WHERE d.id = p.id
        RETURNING d.id, d.vault_id, d.path, d.external_blob, d.title,
                  d.summary, d.tags, d.doc_type, d.domain,
                  d.llm_retry_count,
                  (SELECT name FROM vaults v WHERE v.id = d.vault_id) AS vault_name
        """,
        BATCH_SIZE, MAX_RETRIES, str(settings.external_git_claim_lookahead_secs),
    )
    return [dict(r) for r in rows]


async def _mark_failure(conn, doc_id, retry_count: int, error: str) -> None:
    delay = next_attempt_delay(retry_count)
    next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    await conn.execute(
        """
        UPDATE documents
           SET llm_retry_count     = llm_retry_count + 1,
               llm_last_error      = $2,
               llm_next_attempt_at = $3
         WHERE id = $1
        """,
        doc_id, error[:500], next_at,
    )


async def _resolve_metadata(vault_name: str, body: str) -> dict:
    """One LLM call per doc body. Returns the canonicalised metadata dict."""
    user_msg = (
        f"Vault: {vault_name}\n\n"
        f"Document body (truncated to {MAX_BODY_CHARS} chars):\n\n"
        f"{body[:MAX_BODY_CHARS]}"
    )
    raw = await chat_json(system=_SYSTEM_PROMPT, user=user_msg)
    # LLM output is free-form and goes straight into title/summary/tags.
    # If the model ever emits NFD (rare but possible via copy-pasted
    # training data), the downstream index would de-sync from queries.
    raw = to_nfc_any(raw)
    return _canonicalise(raw)


def _canonicalise(raw: dict) -> dict:
    """Coerce LLM output into the columns we store. Anything malformed
    becomes None/[] rather than raising — the worker would otherwise
    keep retrying forever on a single bad model output."""
    summary = raw.get("summary")
    if isinstance(summary, list):
        summary = " ".join(str(s) for s in summary)
    if summary is not None and not isinstance(summary, str):
        summary = str(summary)

    tags_raw = raw.get("tags") or []
    if isinstance(tags_raw, str):
        tags_raw = [t.strip() for t in tags_raw.split(",")]
    tags = [str(t).strip().lower().replace(" ", "-") for t in tags_raw if str(t).strip()][:10]

    doc_type = raw.get("doc_type")
    if isinstance(doc_type, str) and doc_type.lower() in _DOC_TYPES:
        doc_type = doc_type.lower()
    else:
        doc_type = None

    domain = raw.get("domain")
    if domain is not None and not isinstance(domain, str):
        domain = str(domain)
    if isinstance(domain, str):
        domain = domain.strip()[:64] or None

    return {"summary": summary, "tags": tags, "doc_type": doc_type, "domain": domain}


async def _process_once() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            batch = await _claim_batch(conn)

    if not batch:
        return 0

    git = GitService()
    doc_repo = DocumentRepository(pool)
    succeeded = 0

    for row in batch:
        doc_id = row["id"]
        blob_sha = row["external_blob"]
        vault_name = row["vault_name"]
        if not blob_sha or not vault_name:
            # Either field missing means this row can never make progress
            # — burn retries to MAX in one go so the worker stops picking it.
            reason = "no external_blob" if not blob_sha else "vault gone"
            async with pool.acquire() as conn:
                await _mark_failure(conn, doc_id, MAX_RETRIES - 1, reason)
            continue

        try:
            # `cat-file` forks a subprocess — wrap in `to_thread` so we
            # don't block the event loop.
            raw_bytes = await asyncio.to_thread(git.cat_blob, vault_name, blob_sha)
            body = raw_bytes.decode("utf-8", errors="replace")
            fields = await _resolve_metadata(vault_name, body)
        except LLMPermanentError as e:
            # Deterministic LLM failure — retrying will produce the same
            # result. Burn straight to MAX so the worker stops picking it.
            async with pool.acquire() as conn:
                await _mark_failure(conn, doc_id, MAX_RETRIES - 1, str(e))
            continue
        except (LLMError, RuntimeError, OSError) as e:
            async with pool.acquire() as conn:
                await _mark_failure(conn, doc_id, row["llm_retry_count"], str(e))
            continue

        await doc_repo.mark_llm_metadata_filled(
            doc_id=doc_id,
            summary=fields["summary"],
            tags=fields["tags"],
            doc_type=fields["doc_type"],
            domain=fields["domain"],
            now=datetime.now(timezone.utc),
        )
        succeeded += 1

    return succeeded


_runner = BackfillRunner("metadata_worker", _process_once)
start = _runner.start
stop = _runner.stop


async def pending_stats(vault_id: "uuid.UUID | None" = None) -> dict:
    """Snapshot for /health.

    Operates on the documents table directly (one row per doc with
    llm_metadata_at + llm_retry_count) — vault_id has been on this
    table since day one, so the vault overload is a single clause.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if vault_id is None:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL)
                                                                                      AS pending,
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL
                                     AND llm_retry_count > 0 AND llm_retry_count < $1) AS retrying,
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL
                                     AND llm_retry_count >= $1)                        AS abandoned
                  FROM documents
                """,
                MAX_RETRIES,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL)
                                                                                      AS pending,
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL
                                     AND llm_retry_count > 0 AND llm_retry_count < $1) AS retrying,
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL
                                     AND llm_retry_count >= $1)                        AS abandoned
                  FROM documents
                 WHERE vault_id = $2
                """,
                MAX_RETRIES, vault_id,
            )
    return {
        "pending":   int(row["pending"]),
        "retrying":  int(row["retrying"]),
        "abandoned": int(row["abandoned"]),
    }
