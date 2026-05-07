# Vault-scoped indexing health — design

**Date**: 2026-04-29
**Status**: Approved (brainstorming)
**Owner**: kwoo24
**Related**: prior decision on global IndexingBadge placement (commit `131f445`)

## Problem

The existing `IndexingBadge` in the global header coord strip reflects a system-wide aggregate from `/health` — it sums pending counts across every vault. When the badge was originally placed on each vault overview page, users naturally read it as "this vault has N pending" when it actually meant "the system has N pending". We moved it to the layout header in `131f445` to remove that confusion, but two needs remain unmet:

- **A. UI authenticity** — a vault page should be able to surface "this vault has N pending" without lying or borrowing the global number.
- **B. Operational debugging** — when a vault owner asks "why isn't my doc showing up in dense search yet?", they need a way to see whether the embed/qdrant/metadata pipelines for that vault are stuck or making progress.

Both motivations together require a new endpoint that returns a per-vault snapshot, plus UI surfaces for it. The global indicator stays — it serves a different audience (uptime/health monitoring, multi-vault operators).

## Non-goals

- Vault-scoped `external_git_poller` stats (poller is per-external-repo, not per-vault — the concept doesn't map).
- Vault-scoped `qdrant.reachable` or `bm25_vocab_size` (system-wide concepts).
- Real-time push/SSE — 15s polling matches the existing `useHealth` cadence and is sufficient.
- Caching layer — PG `COUNT(*)` over an indexed column is sub-millisecond; cache adds staleness without measurable benefit.
- Refactoring the global `/health` endpoint — explicitly stays unauthenticated for k8s/uptime probes.

## Architecture

```
                                    ┌─────────────────────┐
                                    │  GET /health        │  unauthenticated, system-wide
                                    │  (existing)         │  → layout coord strip
                                    └─────────────────────┘  (no change)
                                              │
                                              ▼
                          ┌─────────────────────────────────┐
                          │  pending_stats() — workers      │  global aggregate
                          │  embed_worker / vector_indexer  │  (existing, untouched)
                          │  / metadata_worker              │
                          └─────────────────────────────────┘

                                    ┌─────────────────────┐
                                    │  GET /health/vault/{n}  │  authenticated, reader role
                                    │  (NEW)              │  → vault overview badge
                                    └─────────────────────┘   + vault settings diagnostics
                                              │
                                              │ vault_id from check_vault_access
                                              ▼
                          ┌─────────────────────────────────┐
                          │  vault_health(vault_id)         │  per-vault snapshot
                          │  → fan-out to 3 workers         │
                          │    pending_stats(vault_id)      │
                          └─────────────────────────────────┘
                                              │
                                              ▼
                          ┌─────────────────────────────────┐
                          │  chunks.vault_id (NEW column)   │  denormalized, indexed
                          │  + write_source_chunks updates  │
                          └─────────────────────────────────┘
```

### Why denormalize `chunks.vault_id`

The alternative is a polymorphic JOIN per call:

```sql
SELECT COUNT(*) FROM chunks c
WHERE c.embedding IS NULL
  AND ((c.source_type='document' AND c.source_id IN (SELECT id FROM documents WHERE vault_id=$1))
    OR (c.source_type='table'    AND c.source_id IN (SELECT id FROM vault_tables WHERE vault_id=$1))
    OR (c.source_type='file'     AND c.source_id IN (SELECT id FROM vault_files WHERE vault_id=$1)))
```

Rejected because:
- Three subqueries per call. With ~10 vault pages × 15s polling × multiple users, this is hundreds of joins per minute even at light load.
- The query plan depends on optimizer behavior across three branches.
- The `edges` table already follows the same pattern (`vault_id` column denormalized at write time) — consistent with project precedent.

A nullable column + backfill + NOT NULL + FK + index is a one-shot operational cost; the per-call cost forever after is sub-millisecond.

## Schema migration

New file: `backend/app/db/migrations/0NN_chunks_vault_id.py`

```python
async def _run(conn):
    async with conn.transaction():
        # 1. Add nullable column — concurrent INSERT continues to work
        await conn.execute(
            "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS vault_id UUID"
        )

        # 2. Backfill from parent tables per source_type
        await conn.execute("""
            UPDATE chunks c
               SET vault_id = d.vault_id
              FROM documents d
             WHERE c.source_type = 'document'
               AND c.source_id = d.id
               AND c.vault_id IS NULL
        """)
        await conn.execute("""
            UPDATE chunks c
               SET vault_id = t.vault_id
              FROM vault_tables t
             WHERE c.source_type = 'table'
               AND c.source_id = t.id
               AND c.vault_id IS NULL
        """)
        await conn.execute("""
            UPDATE chunks c
               SET vault_id = f.vault_id
              FROM vault_files f
             WHERE c.source_type = 'file'
               AND c.source_id = f.id
               AND c.vault_id IS NULL
        """)

        # 3. Orphan cleanup — chunks whose source row was deleted before
        #    chunk-delete cascade caught up. Already invisible to consumers.
        orphans = await conn.fetchval(
            "SELECT COUNT(*) FROM chunks WHERE vault_id IS NULL"
        )
        if orphans:
            logger.warning("Deleting %d orphan chunks with no parent", orphans)
            await conn.execute("DELETE FROM chunks WHERE vault_id IS NULL")

        # 4. Lock down: NOT NULL + FK + index
        await conn.execute(
            "ALTER TABLE chunks ALTER COLUMN vault_id SET NOT NULL"
        )
        await conn.execute("""
            ALTER TABLE chunks
              ADD CONSTRAINT chunks_vault_id_fkey
              FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_vault_id
                ON chunks (vault_id)
        """)
```

Single transaction so a concurrent INSERT during steps 1–2 can't land a NULL row that crashes step 4. `init.sql` is updated in the same change so a fresh DB has the column natively (no migration race).

`write_source_chunks` signature gains a required `vault_id` argument; all 5 callers (`document_service.put` / `update` / `replace`, `file_service.upsert_file_index`, `table_service.upsert_table_index`) already know the vault and pass it through.

## Service layer

Each of the 3 background workers gets a `vault_id` overload on its existing `pending_stats()` function. Without the argument: global aggregate (current behavior, used by `/health`). With it: vault-scoped subset.

```python
async def pending_stats(vault_id: uuid.UUID | None = None) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if vault_id is None:
            row = await conn.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE embedding IS NULL)                                AS pending,
                    COUNT(*) FILTER (WHERE embedding IS NULL AND embed_retry_count > 0
                                     AND embed_retry_count < $1)                              AS retrying,
                    COUNT(*) FILTER (WHERE embedding IS NULL AND embed_retry_count >= $1)    AS abandoned
                  FROM chunks
            """, MAX_RETRIES)
        else:
            row = await conn.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE embedding IS NULL)                                AS pending,
                    COUNT(*) FILTER (WHERE embedding IS NULL AND embed_retry_count > 0
                                     AND embed_retry_count < $1)                              AS retrying,
                    COUNT(*) FILTER (WHERE embedding IS NULL AND embed_retry_count >= $1)    AS abandoned
                  FROM chunks
                 WHERE vault_id = $2
            """, MAX_RETRIES, vault_id)
    return {
        "pending": int(row["pending"]),
        "retrying": int(row["retrying"]),
        "abandoned": int(row["abandoned"]),
    }
```

Same pattern for `vector_indexer.pending_stats` and `metadata_worker.pending_stats`. Two explicit branches instead of `WHERE vault_id = $X OR $X IS NULL` because the conditional form prevents PG from using the partial index.

New helper `app/services/health.py::vault_health(vault_id)` aggregates the three:

```python
async def vault_health(vault_id: uuid.UUID) -> dict:
    embed = await embed_worker.pending_stats(vault_id)
    qdrant_backfill = await vector_indexer.pending_stats(vault_id)
    metadata = await metadata_worker.pending_stats(vault_id)
    return {
        "embed_backfill": embed,
        "metadata_backfill": metadata,
        "qdrant": {"backfill": qdrant_backfill},
    }
```

Sequential awaits initially; `asyncio.gather` is a one-line change later if PG round-trip latency becomes visible.

## API endpoint

`GET /health/vault/{name}` — new route in `main.py` next to the existing `/health` (or `app/api/routes/health.py` if we extract first).

```python
@app.get("/health/vault/{name}", summary="Per-vault indexing health")
async def vault_health_route(
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, name, required_role="reader")
    return {"vault": name, **await vault_health(access["vault_id"])}
```

### Auth model — intentional asymmetry

| Endpoint | Auth | Rationale |
|---|---|---|
| `GET /health` | unauthenticated | k8s probes, uptime monitoring; system-wide aggregate doesn't leak which vaults exist |
| `GET /health/vault/{name}` | reader required | vault existence itself is information; unauthenticated access would let anyone probe "does vault X exist?" — same principle as the KG access fix in #3 |

`check_vault_access` already enforces archived/external-git/public_access semantics correctly. 404 on unknown vault, 403 on missing role, 401 on missing auth header.

### No `_safe()` wrapper

The global `/health` wraps each worker's `pending_stats` in `_safe()` because uptime monitoring should never receive a 500 — a single crashed worker shouldn't crash the dashboard. The vault endpoint is user-facing data; if PG is down or a worker query throws, returning 500 (and letting the frontend skip rendering the badge) is cleaner than silently returning `{"error": "..."}` masquerading as data.

### No caching, no rate limit

PG `COUNT(*)` over `idx_chunks_vault_id` is sub-millisecond; 15s × concurrent users << 10 RPS in current operational envelope. Caching adds staleness ("why didn't my number drop after I waited?") without measurable savings. Rate limiting isn't applied to the rest of `/api/v1/*` either — adding it here would break the project's consistency.

### Response shape

```json
{
  "vault": "gnu",
  "embed_backfill":    { "pending": 3, "retrying": 0, "abandoned": 0 },
  "metadata_backfill": { "pending": 0, "retrying": 0, "abandoned": 0 },
  "qdrant": {
    "backfill": {
      "upsert": { "pending": 1, "retrying": 0, "abandoned": 0, "indexed": 0 },
      "delete": { "pending": 0, "abandoned": 0 }
    }
  }
}
```

A subset of the global `/health` shape (no `qdrant.reachable`, no `bm25_vocab_size`, no `external_git`, no top-level `status`). The frontend `HealthSnapshot` interface declares all fields optional, so it can be reused as-is.

## Frontend

### Hook

`frontend/src/hooks/use-vault-health.ts` mirrors `useHealth` with a vault argument and an early return when no token is present (the endpoint requires auth, so unauthenticated polling would just produce 401s).

```ts
export function useVaultHealth(
  vaultName: string | undefined,
  intervalMs: number = 15000,
) {
  const [data, setData] = useState<VaultHealthSnapshot | null>(null);
  useEffect(() => {
    if (!vaultName || !getToken()) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const snapshot = await api<VaultHealthSnapshot>(`/health/vault/${vaultName}`);
        if (!cancelled) setData(snapshot);
      } catch {
        /* silent — IndexingBadge falls back to placeholder */
      }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => { cancelled = true; clearInterval(id); };
  }, [vaultName, intervalMs]);
  return data;
}
```

The shared `lib/api.ts` `api()` helper supplies the auth header automatically and handles 401 by redirecting to `/auth`.

### UI surface 1 — vault overview badge

`pages/vault.tsx` adds an `<IndexingBadge pending={vaultPending} />` to the metadata badge row, after `RoleBadge` / `VaultStateBadge` and before the `MEMBERS` / `SETTINGS` links.

The same `IndexingBadge` component is now used in two placements with different sources:
- Layout coord strip — global aggregate
- Vault overview metadata row — vault-scoped

When both are non-zero, the user sees them as two distinct numbers next to two distinct contexts (system identity vs vault identity). Same visual chrome, different semantic placement.

### UI surface 2 — vault settings diagnostics section

`pages/vault-settings.tsx` gains a `§ DIAGNOSTICS` section below `§ LIFECYCLE`, rendering only when `vaultHealth` has resolved. Three columns side by side: EMBED, QDRANT, METADATA. Each shows `pending` / `retrying` / `abandoned` in tabular monospace. A short paragraph below explains how to read the numbers — "non-zero values across multiple refreshes signal a stuck worker; check embedding API or Qdrant".

This surface serves motivation B (operational debugging) without cluttering the typical user's view. The settings page is owner-gated, so the diagnostics audience is exactly the audience that has authority to act on the data.

### Polling discipline

Each page that calls `useVaultHealth(name)` polls independently while mounted. Navigating between vault overview and vault settings unmounts one and mounts the other — the polling timer follows the visible page, not duplicated. The hook's cleanup `clearInterval` on unmount prevents leaks.

## Edge cases

| Case | Behavior |
|---|---|
| Empty vault (zero chunks) | `pending=0` → `IndexingBadge` doesn't render; DIAGNOSTICS shows 0/0/0 |
| Archived vault | Reader access still allowed; numbers reflect any backfill still running on existing chunks |
| Migration in progress, concurrent INSERT | Single transaction prevents NULL row from leaking past `SET NOT NULL`. Code deploy must precede migration so writers always supply `vault_id`. |
| Cross-vault edges (KG concept) | Doesn't apply — chunks belong to exactly one source row in exactly one vault. No leakage path. |
| Worker processes a chunk during polling | Worker's UPDATE sets `embedding`/`indexed_at` non-NULL; next poll naturally reflects the lower count. No coordination needed. |
| Caller loses vault access mid-session | Next poll returns 403 → silent catch → `IndexingBadge` shows placeholder. The existing 401 flow handles "session lost entirely" by redirecting to `/auth`. |
| `chunks.vault_id` index degrades on very large tables | Out of scope. Partial index `WHERE embedding IS NULL` would be the next step if needed. |

## Deploy order

1. **Code deploy** — `write_source_chunks` accepts `vault_id`; all 5 callers pass it. Column doesn't exist yet, so the new INSERT branch is gated on the migration. Practical implementation: code deploy + migration in the same release; rolling deploys handle the brief overlap because the column is added before the new code path requires it (see step 2).
2. **Migration** — adds nullable column → backfill → SET NOT NULL → FK → index. Inside one transaction.
3. **API + frontend deploy** — new endpoint and UI go live. If the frontend ships before the endpoint, the new request gets 404, the silent-catch path takes over, and no UI breakage results.

The risky inversion is migration-before-code: existing writers would suddenly hit `NOT NULL` violations. The migration script's first step (nullable column) protects against that during the transaction window itself; the deploy script must still order code before migration to keep writers correct between releases.

## Testing

### Backend

E2E additions to `backend/tests/test_security_edge_e2e.sh`:

```bash
# user1 sees own vault health
R=$(acurl_as "$PAT1" "$BASE_URL/api/v1/health/vault/$VAULT1")
HAS=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('embed_backfill' in d)")
[ "$HAS" = "True" ] && pass "User1 sees own vault health"

# user2 blocked on user1's private vault → 403
echo "$(acurl_as "$PAT2" "$BASE_URL/api/v1/health/vault/$VAULT1" -w "%{http_code}")" | grep -q 403 \
  && pass "User2 blocked from vault health"

# unauthenticated → 401
echo "$(curl -sS "$BASE_URL/api/v1/health/vault/$VAULT1" -w "%{http_code}")" | grep -q 401 \
  && pass "Unauth blocked"

# unknown vault → 404
echo "$(acurl_as "$PAT1" "$BASE_URL/api/v1/health/vault/nonexistent-xyz" -w "%{http_code}")" | grep -q 404 \
  && pass "Unknown vault returns 404"
```

No new pure-unit tests for `pending_stats` — the existing global `pending_stats` has none either; consistency over coverage in this layer.

Migration sanity: post-deploy, `SELECT COUNT(*) FROM chunks WHERE vault_id IS NULL` must return 0.

### Frontend

No new vitest tests — `useHealth` doesn't have one either, and writing a polling-hook timer test costs more than the bug surface justifies. Verification by Playwright on the dev server: vault overview shows the badge in the metadata row; vault settings shows the diagnostics section; both populate after the first poll.

## Open questions / future work

- **Partial index on `embedding IS NULL`** — if `chunks` grows past O(100k) and counting becomes slow, switch from `idx_chunks_vault_id (vault_id)` to `idx_chunks_vault_id_pending (vault_id) WHERE embedding IS NULL`. Defer.
- **External git poller per-vault** — if external mirror polling becomes vault-scoped, expose its stats here too. Currently aggregated.
- **System-only DIAGNOSTICS for global** — admins might want the same DIAGNOSTICS shape on a global page. Out of scope; settings → admin tab could host it later.
- **Vault stats invalidation on bulk delete** — when a vault is deleted, its chunks cascade-delete. The badge naturally returns to 0 after the cascade completes; no explicit invalidation needed.
