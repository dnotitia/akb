---
status: accepted
stage: implemented
created: 2026-09-14
updated: 2026-09-14
---

# Compact indexing status

## Decision

Keep indexing as a small contextual signal, not a new operations interface.
After reviewing the first implementation, the user rejected its persistent
Search status control, global panel, and extra Home notice as excessive.
The shared observation and backend correctness improvements remain; the visible
interface returns to pending-count badges.

| Surface | Current design |
| --- | --- |
| Global header | Passive `N indexing` badge immediately before Search, from `lg` only |
| Vault Overview | The same passive badge for that Vault alone |
| Vault Settings → Operations | Read-only per-stage counts, modes, and observation time |
| Home body and profile menu | No additional indexing interface; existing connection guide is unchanged |

Badges disappear when no fresh positive indexing count is known. There is no
idle slot, dropdown, modal, click action, or mobile profile-menu substitute.
Existing header Search/profile sizing and Home content layout are preserved.

## Count and accessibility rules

- Sum only fresh `search_index.pending` values in **chunks**, never document
  totals, metadata jobs, File projections, revision updates, or affected Vaults.
- Retrying is already a subset of pending. Do not add it or subtract abandoned.
- Accept versioned counts only when the stage is enabled. Omit stale,
  unavailable, invalid, unknown-mode, and paused versioned observations.
- Recognizable legacy pending counts remain supported even without mode metadata.
- With partial indexing coverage, show a positive known subtotal as `N+ indexing`.
  The title and screen-reader text explain “at least” and unavailable counts;
  do not rely on the plus sign or hover alone to communicate its meaning.
- A hidden badge means no fresh positive count is available, not “all indexed,”
  healthy search, completed saving, or guaranteed dense-search coverage.
- Use existing semantic pending tokens, a small labelled progress glyph, tabular
  numerals, and reduced-motion support. The header owns polite announcements;
  the scoped Overview badge remains ordinary text.

Detailed stage semantics, historical versus current-Head failures, and legacy
limitations remain visible in Operations, rather than expanding global chrome.
See [the data contract](data-contract.md) and
[the design system](../../../../frontend/DESIGN_SYSTEM.md).

## Implementation boundaries

- [Header](../../../../frontend/src/components/header-indexing-status.tsx) and
  [Overview](../../../../frontend/src/pages/vault.tsx) compose
  [PendingIndexingBadge](../../../../frontend/src/components/pending-indexing-badge.tsx).
- [Vault Settings](../../../../frontend/src/pages/vault-settings.tsx) composes
  [SearchStatusStages](../../../../frontend/src/components/search-status-stages.tsx).
- [The normalizer](../../../../frontend/src/lib/indexing-status.ts) keeps stage
  units and freshness separate; its pending selector supplies badge counts.
  Existing affected-Vault sets remain internal scheduling/state data, not badge text.
- [The shared store](../../../../frontend/src/lib/search-status-store.ts) and
  [provider](../../../../frontend/src/hooks/use-search-status.tsx) retain
  authenticated, account-scoped polling, bounded requests, stale detection,
  access revocation, and legacy compatibility. No component starts another timer.
- [Per-Vault health](../../../../backend/app/services/health.py) retains existing
  keys and reader authorization with an additive versioned interpretation
  envelope. Worker gates are behavior-preserving; no migration is introduced.

No new dependency, aggregate endpoint, global `/health` or `/stats` fallback,
SSE/WebSocket, retry/reindex mutation, completion percentage, or status storage
in the browser is introduced.

## Verification and deployment record

The retained backend/model/store implementation passed focused backend tests,
isolated PostgreSQL current-Head/isolation tests, and the repository check gate.
The earlier implementation was deployed to **local Compose only**; readiness and
the deployed MCP/REST boundary suite passed (14 tests). Production was not changed.

The revised badge UI passed the frontend token guard, typecheck, lint, and full
unit suite (129 files, 1,039 tests). Existing lint warnings remain. Eleven isolated
browser tests cover 375/768/1024/1440/2560 CSS-pixel layouts in both themes,
reduced motion, stable profile geometry, idle hiding, and Overview-versus-workspace
counts; screenshots were inspected. The tests use synthetic HTTP responses,
not live worker transitions, production data, or assistive-technology user testing.
Removed-panel interaction tests are historical evidence, not current acceptance.
The badge revision was then deployed by rebuilding and replacing the local
frontend only: HTTP 200 and the served bundle were verified. Existing healthy
backend/worker containers and production were left unchanged.

## Alternatives not retained

- A persistent Search status control and global detail panel: too much chrome
  for the user's routine need to see whether indexing is pending.
- Restoring a Home statistics rail or a processing notice: duplicates context
  and interrupts the personal working set.
- Summing all processing stages: mixes units and can count the same Resource
  more than once. Operations keeps those values separate.
- Putting processing into the notification bell: conflates queue state with
  personal events and unread messages.
