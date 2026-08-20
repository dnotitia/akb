# Brainstorm decision log (2026-08-19, PM session)

Questions asked one at a time; PM picked the recommended option in each case.

## 1. Injection mechanism

- **Chosen: tool-response injection.** First tool call touching a vault in a
  session carries the skill; tracked per (session, vault, skill-version) so a
  mid-session skill edit re-injects on the next call (PM raised the
  long-lived-session case explicitly; version tracking answers it).
- Rejected: embedding bodies in `initialize.instructions` (connection is not
  vault-scoped; token-heavy; stale until reconnect). Rejected: keeping the
  pointer-only status quo (agents that skip `akb_help` never see the skill).

## 2. Enforcement direction

- **Chosen: two-way** — overview is skill-only AND skill type exists only at
  the canonical path (one per vault). One-way or create-only variants leave
  the dual-keying divergence (audit) reachable through other verbs.

## 3. UI surface

- **Chosen: system collection + dedicated settings editor.** Tree keeps the
  collection visible (locked, distinct) for git/browse transparency; editing
  is unified in vault settings.
- Rejected: hiding from the tree (UI would contradict `akb_browse`/git).
  Rejected: status quo with partial fixes (editing stays scattered).

## 4. Survival invariant

- **Chosen: delete forbidden, reset-to-template only.** Simplifies injection
  and help (missing branch becomes mirror-only), removes create-CTA states.
- Rejected: keep deletable (keeps every missing branch alive). Rejected:
  disable toggle (unproven need, extra state).

## 5. Existing violations

- **Chosen: automatic migration** via service-layer backfill (moves with
  aliases, retypes, reseeds), preceded by a prod count recorded privately.
- Rejected: grandfathering (legacy branches in every guard, confusion
  persists). Rejected: deferring the decision (design left incomplete).

## Approach selection

- **A (reserved-path policy module) chosen** over B (de-document the skill)
  and C (first-class system-collection DB concept). Deciding fact from the
  audit: the native backend creates no collections row for the seed, so only
  a path-namespace rule applies identically to both backends.
