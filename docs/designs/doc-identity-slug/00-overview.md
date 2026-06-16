# Document identity & slug — `{slug}-{shortid}` scheme

Status: **accepted** · Scope: documents (Phase 1 shipped); tables/files/rename are follow-on phases.

## Problem

A document's identity is its `path` = `{collection}/{_slugify(title)}.md`, frozen at
create with `UNIQUE(vault_id, path)`. Two failure modes followed from deriving the
unique key directly from the human title:

1. **False-duplicate rejection on create.** Distinct human titles that normalize to
   the same slug (`"API Guide"` vs `"Api  Guide"` vs `"api-guide"`) collide on one
   path and the second create is rejected as "already exists" — even though the user
   meant two different documents.
2. **Empty / symbol-only titles** (`"!!!"`, `"---"`, emoji) slugify to `""`, yielding
   a degenerate path `"{collection}/.md"` (a dotfile); the first wins, the rest collide.
3. **80-char truncation collisions** — two long titles differing only past char 80
   produce the same slug.

(Editing a title afterwards is *not* the bug — title is a mutable, non-unique label by
design; identity stays on `path`. See "identity vs label" below.)

## Research → decision

Surveyed how mature systems resolve slug/name collisions (Notion, Medium, dev.to,
Stack Overflow, Linear/Jira, YouTube, WordPress, Ghost, Confluence, MediaWiki). Three
families:

- **Embedded id/hash** (Notion `{slug}-{uuid}`, Medium `{slug}-{12hex}`, dev.to
  `{slug}-{rand}`, SO `/questions/{id}/{slug}`): an id in the handle makes the slug
  cosmetic, so collisions are **impossible by construction** — no check, no reject.
- **Numeric suffix** (WordPress/Ghost `-2`, file managers `(2)`): slug *is* the key,
  so collisions get a counter.
- **Reject** (Confluence, MediaWiki): title *is* identity, duplicates forbidden.

AKB stores docs as real git files, so a fully opaque UUID filename would wreck git
readability. AKB's `path` (with `UNIQUE(vault_id, path)`, resolved by `find_by_ref`
on `d.path`) **is the identity** — there is no separate id in the handle. A second
research pass (WordPress `wp_unique_post_slug`, Drupal Pathauto, Rails `friendly_id`,
Django, Ghost — all source-confirmed) established the rule:

> When the slug/path **is** the identity, the standard pattern is to emit the **clean
> slug** and disambiguate with a suffix **only on collision** — never unconditionally.
> Unconditional id-embedding is only idiomatic when a *separate* durable id is the
> identity and the slug is cosmetic (Notion/Medium/SO/YouTube).

**Decision: conditional `-{shortid}` on collision.**

```
path = {collection}/{slug}.md                 # clean path when the slug is free
     = {collection}/{slug}-{shortid}.md       # ONLY when {slug}.md is already taken
                                              # shortid = first 8 hex of the doc UUID
```

The clean path stays predictable & human-readable (AKB's git-native value, and what
the e2e suite + agents rely on); duplicate-title puts still succeed (suffixed) instead
of erroring. The doc UUID makes the suffixed path unique by construction;
`UNIQUE(vault_id, path)` is the final guard. The collision check + suffix run under
the existing `(vault, base_path)` advisory lock, so concurrent same-slug puts serialize
correctly.

### Pinned-slug exception (stable seeds)

An **explicit** `req.slug` means the caller wants a *predictable, exact* path (e.g. the
vault-skill seed pins `overview/vault-skill.md` so `akb_help(topic="vault-skill")`
resolves). Explicit slugs are never suffixed — a genuine collision on a pinned slug
**rejects** (correct). The shortid is appended **only when the slug is derived from the
title and its clean path is already taken.**

## Identity vs label (the contract, now explicit)

- **Durable identity** = `documents.id` (UUID PK) + its rendered handle `path`
  (akb:// URI). Frozen at create.
- **Human label** = `title` — freely mutable, **non-unique**, never re-derives the path.
  Two docs may share a title (esp. across collections); uniqueness lives only on
  `path`, never on `title`. UI must render the live `title`, never reparse the slug.

## Per-resource scope

| Resource | Identity | Collision policy | This design |
|---|---|---|---|
| **document** | `path` (UUID behind it) | title-derived → `-{shortid}` (no reject); explicit slug → reject | **Phase 1 (this change)** |
| **table** | `name` (raw) | reject (name is the SQL handle) | unchanged |
| **file** | opaque id / `s3_key` | n/a | unchanged |
| **collection** | `path` (folder) | idempotent reuse (`get_or_create`) | unchanged — same folder must dedupe, never suffix |

Deliberately differentiated by storage backend (git / PG / S3), unified only at the
mechanism layer (one hardened `slugify`).

## Phases

- **Phase 1 (shipped here):** hardened `slugify` (empty→`untitled`, trim dangling
  hyphens, truncate-then-trim) + **on-collision** `-{shortid}` for title-derived paths
  + thread the doc UUID so the suffix matches the row id. No migration, no URI grammar
  change, no client break — existing docs keep their paths; unique titles still get
  clean paths; only a colliding title gains a shortid. Fixes failure modes 1–3.
  Test impact: assertions that predict a *unique* clean path still pass; the
  conflict-atomicity e2e test flips intent from "2nd put errors" to "2nd put succeeds
  at a distinct suffixed path, both bodies preserved" (capture the returned uri/path
  rather than predicting it, per MCP rule "use returned URIs").
- **Phase 2 (shipped):** document rename/move — polymorphic `resource_aliases(vault_id,
  resource_type, old_ref, resource_id UUID)` keyed to the UUID (never to a path, so no
  redirect chains), `GitService.move_file` (`git mv` + `git log --follow`), a
  `find_by_ref` alias arm (exact match first, alias fallback) so old akb:// links keep
  resolving, edges/publications rewritten old→new URI on move, chunk re-index with the
  new path header, alias cleanup on delete. Exposed as the `akb_move` MCP tool
  (collection and/or slug; on-collision suffix reuses Phase 1 rule; the doc UUID is
  immutable so a move never changes identity). REST endpoint + frontend move UI: TODO.
- **Phase 3 (follow-on):** generalize rename to tables (ALTER + role re-grant) and
  recursive collection move; file rename is a trivial name update.

## Correctness review (Phase 2)

An external-practice + adversarial code review validated the three core choices as
**correct**: own-uuid suffix on collision (Notion `{slug}-{id}` + git's auto-lengthening
short-hash precedent), id-keyed alias (avoids MediaWiki's double-redirect chains),
`git mv` + `git log --follow` (the standard rename-history mechanism). Fixes applied
from the review: edges rewrite is now `UNIQUE(source,target,relation)`-safe (move
non-colliding rows, drop the duplicates); the move target path is recomputed from the
fresh row after the lock (stale-read race); the alias fallback re-verifies `vault_id`;
a re-index miss after `git mv` is logged, not silently swallowed.

### Accepted trade-offs / known limitations
- **Body link text is not rewritten on move.** A move rewrites graph *edges* and
  publications old→new, but the markdown/wikilink text inside *other* documents'
  bodies still shows the old URI — it keeps resolving via the alias, and the edge is
  re-derived correctly on the next re-index. Rewriting body content would alter
  author-written text, so it's intentionally left.
- **Aliases are durable** (dropped only on path-reclaim or resource delete) — matching
  the SEO/redirect canon ("keep redirects as long as anyone might reference them"). No
  TTL/GC for now.
- **`git log --follow` is single-file**, so a future collection move (Phase 3) must
  follow each doc individually.
- **Suffixed-path concurrency** rests on uuid global-uniqueness + `UNIQUE(vault_id,
  path)` as the final arbiter (suffixed candidates aren't separately locked); the
  residual concurrent race surfaces as a 409, not corruption — the same risk profile as
  put/update.

## Rejected alternatives

- **Opaque-UUID-primary everywhere (Notion-pure):** demotes the git tree from
  source-of-truth to a derived projection; heavy migration of edges/publications.
  Rejected — contradicts AKB's git-native concept.
- **Numeric `-2` suffix on collision:** the WordPress/Ghost variant — also valid, but
  needs a DB-scan loop (`-2`, `-3`, …) to find a free slot. The doc's short id is a
  single deterministic disambiguator (no scan) and ties the path to the row. Same
  conditional trigger, simpler resolution.
- **Unconditional `{slug}-{shortid}` (always append):** considered and rejected after
  research — no mainstream framework embeds an id unconditionally into an
  identity-bearing path; it destroys clean-path predictability for the ~99%
  no-collision case to solve a problem the `UNIQUE` constraint + on-collision suffix
  already solves, and would break every path-predicting caller/test.
