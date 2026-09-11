"""Document counts and freshness timestamps, resolved against the active authority.

akb#525. The legacy `documents` catalog is written only by the bare-Git
document path. On `postgres_native` the native path writes
`native_resources` / `native_revisions` and never touches `documents`, so
every count or "when was this last written" question answered from the
catalog freezes at the cutover: a vault created afterwards reads 0/NULL, and
a vault that predates it keeps a believable but motionless number.

The catalog is not the only frozen surface. `collections.doc_count` and
`collections.last_updated` are denormalised counters maintained by
`DocumentRepository.increment_count` / `decrement_count`, which only the
legacy write path calls — so `akb_browse` shows the same stall one level
down, and a grep for `FROM documents` does not find it.

This module is the single place that decides which authority answers such a
question, so a new counter surface has one import to reach instead of its own
copy of the branch. It answers only the counting/freshness questions; the
identity, body and path questions each backend already owns are not here.

Three shapes, because the call sites differ:

* SQL constants for callers that fan a query out themselves
  (`access_service.get_vault_info` gathers eight counts in parallel;
  `app/stats/sampler` runs its counts inside one repeatable-read snapshot).
* `collection_document_totals`, which executes, because the browse view needs
  a map keyed by collection path rather than one scalar.

Native semantics, and how they line up with the legacy ones:

* A document is a `native_resources` row with `surface = 'document'` and
  `lifecycle = 'live'`. Deleting removes the legacy row and flips the native
  lifecycle, so both authorities count the same population. `lifecycle` does
  NOT encode archival — an archived document keeps `status: archived` in its
  frontmatter and stays live here, matching the legacy catalog, which counts
  archived rows too.
* Last activity comes from the head revision. `NativeRevisionRepository.set_head`
  writes `native_resources.updated_at = <the head revision's occurred_at>` in
  the same statement that advances `head_revision_id`, so ordering resources by
  `updated_at` and reading the actor off the head revision returns one
  consistent (time, actor) pair. Measured on one installation: 136,188 of
  136,188 live document resources had a non-null head whose `occurred_at`
  equalled `updated_at` and was the newest revision of that resource.

  Ordering on the resource side is also what keeps the query bounded by the
  vault. Sorting `native_revisions.occurred_at` across a namespace's live
  resources makes the planner scan the WHOLE revision ledger — every vault's
  history, append-only and never pruned. Measured on the same installation,
  for a 50,850-document vault: 93.7 ms scanning all 137,658 revisions, against
  60.9 ms for the form below, which touches only this vault's resources. (The
  legacy query it replaces was 938 ms — a bitmap heap scan over the wide
  catalog rows. Neither native form is a regression against it.)
"""

from __future__ import annotations

import uuid
from datetime import datetime

# ── vault document count ─────────────────────────────────────

_VAULT_COUNT_LEGACY = "SELECT COUNT(*) FROM documents WHERE vault_id = $1"
_VAULT_COUNT_NATIVE = (
    "SELECT COUNT(*) FROM native_resources "
    "WHERE namespace_id = $1 AND surface = 'document' AND lifecycle = 'live'"
)

# ── vault last activity ──────────────────────────────────────
#
# Both arms return `updated_at` / `created_by` under those names so the
# response shape of every consumer stays byte-identical across the switch.

_VAULT_LAST_ACTIVITY_LEGACY = (
    "SELECT updated_at, created_by FROM documents WHERE vault_id = $1 "
    "ORDER BY updated_at DESC LIMIT 1"
)
_VAULT_LAST_ACTIVITY_NATIVE = (
    "SELECT r.updated_at AS updated_at, nr.actor AS created_by "
    "FROM native_resources r "
    "JOIN native_revisions nr "
    "  ON nr.resource_id = r.resource_id AND nr.revision_id = r.head_revision_id "
    "WHERE r.namespace_id = $1 AND r.surface = 'document' AND r.lifecycle = 'live' "
    "ORDER BY r.updated_at DESC LIMIT 1"
)

# ── instance-wide corpus count ───────────────────────────────

_INSTANCE_COUNT_LEGACY = "SELECT COUNT(*) FROM documents"
_INSTANCE_COUNT_NATIVE = (
    "SELECT COUNT(*) FROM native_resources "
    "WHERE surface = 'document' AND lifecycle = 'live'"
)

# ── per-collection totals ────────────────────────────────────
#
# `collections.doc_count` counts a collection's DIRECT children only
# (`increment_count` is called with the document's own `collection_id`), so
# this reproduces that and not a subtree total. The parent path is cut off
# each resource path rather than matched with `LIKE c.path || '/%'`: one
# grouped index scan instead of a per-collection predicate, and no LIKE
# metacharacter to escape — `_` is a wildcard and is ordinary in a path.
# A vault-root document has no separator and no collection, and is excluded.

_COLLECTION_TOTALS_NATIVE = """
    SELECT substring(
               r.current_path from 1
               for length(r.current_path) - position('/' in reverse(r.current_path))
           ) AS collection_path,
           COUNT(*) AS doc_count,
           MAX(r.updated_at) AS last_updated
      FROM native_resources r
     WHERE r.namespace_id = $1
       AND r.surface = 'document'
       AND r.lifecycle = 'live'
       AND position('/' in r.current_path) > 0
"""
# Browsing into a subtree only renders the collections under it, so scope the
# scan the same way rather than grouping the whole vault every time. Measured
# on one installation, unscoped on a 50,850-document vault: 118 ms.
_COLLECTION_TOTALS_PREFIX_CLAUSE = "       AND r.current_path LIKE $2 ESCAPE '\\'\n"
_COLLECTION_TOTALS_GROUP_BY = "     GROUP BY 1\n"


def native_documents_are_authoritative() -> bool:
    """True when the native ledger, not the legacy catalog, holds documents.

    Local import avoids a module cycle: `search_service` owns the selector and
    does not import the counter consumers, so importing it here is
    one-directional. This is the same selector the search hydration path uses
    to decide which arm a hit belongs to, so a counter can never disagree with
    what a search over the same corpus returns.
    """
    from app.services.search_service import _configured_document_source_type

    try:
        from app.services.search_service import NATIVE_DOCUMENT_SOURCE

        return _configured_document_source_type() == NATIVE_DOCUMENT_SOURCE
    except RuntimeError:
        # Partial native guard (measurement-only mismatch etc.): fail closed
        # to the legacy catalog rather than raising out of a read path.
        return False


def vault_document_count_sql() -> str:
    """`COUNT(*)` of a vault's live documents, bound to `$1 = vault id`."""
    return _VAULT_COUNT_NATIVE if native_documents_are_authoritative() else _VAULT_COUNT_LEGACY


def vault_last_activity_sql() -> str:
    """Newest `(updated_at, created_by)` in a vault, bound to `$1 = vault id`."""
    return (
        _VAULT_LAST_ACTIVITY_NATIVE
        if native_documents_are_authoritative()
        else _VAULT_LAST_ACTIVITY_LEGACY
    )


def instance_document_count_sql() -> str:
    """`COUNT(*)` of every live document in the installation. No parameters."""
    return (
        _INSTANCE_COUNT_NATIVE
        if native_documents_are_authoritative()
        else _INSTANCE_COUNT_LEGACY
    )


async def collection_document_totals(
    pool, vault_id: uuid.UUID, *, prefix: str = "",
) -> dict[str, tuple[int, datetime | None]] | None:
    """Per-collection `(doc_count, last_updated)` from the native authority.

    Returns ``None`` on a legacy installation, where the stored
    `collections.doc_count` / `collections.last_updated` columns are the
    authority and the caller should keep reading them. The authority is
    resolved before the pool is touched, so the legacy arm costs no
    connection and no round-trip.

    A collection absent from the map holds no live native document directly,
    which is a real zero rather than missing data — callers must render it as
    zero and not fall back to the stored column, since the stored column is
    exactly the frozen number this exists to replace.
    """
    if not native_documents_are_authoritative():
        return None
    sql = _COLLECTION_TOTALS_NATIVE
    params: list = [vault_id]
    if prefix:
        from app.util.text import like_escape

        params.append(like_escape(prefix) + "/%")
        sql += _COLLECTION_TOTALS_PREFIX_CLAUSE
    sql += _COLLECTION_TOTALS_GROUP_BY
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {r["collection_path"]: (int(r["doc_count"]), r["last_updated"]) for r in rows}
