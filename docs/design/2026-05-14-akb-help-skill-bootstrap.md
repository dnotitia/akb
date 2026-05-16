# AKB Help Skill Bootstrap — Design Spec

> Brainstormed 2026-05-14. Supersedes the over-engineered 2026-05-06 design (which proposed `.akb/` local bundles, `akb_bootstrap` MCP tool, CLI materializer, and new REST endpoints — all removed here).

## Goal

Let an LLM agent that connects to AKB via MCP discover *vault-specific writing conventions* in one tool call. The conventions live as a normal AKB document the vault owner authors and maintains; the MCP server only exposes a pointer plus a router.

## Non-goals (cut from earlier design)

- ❌ Local `.akb/` bundle on the user's filesystem (Claude Code's own skill system already covers host-side persistence — AKB shouldn't touch user FS).
- ❌ `akb_bootstrap` MCP tool / `npx akb-mcp bootstrap` CLI (vault-skill is a normal doc, `akb_get` suffices).
- ❌ New REST endpoints (`/help/akb-agent-guide.md`, `/vault-guide.md`) — duplicates existing `akb_get`.
- ❌ Collection-level skill cascade (`{collection}/_skill.md`) — v1.1 if real demand surfaces.
- ❌ Frontmatter `rules` deterministic merge / section-key parsing — v1.1.
- ❌ AKB-built-in default skill content — template seed at vault create time *is* the default; no separate fallback corpus in backend.
- ❌ Dedicated `akb_skill_resolve` tool / graph edges / exemplar tagging — all v1.1+.

## Architecture — 3 layers

```
Layer 1: MCP initialize.instructions  (~80 tokens, every-turn system prompt)
  Tells the agent: "Before writing into a vault, call
  akb_help(topic='vault-skill', vault=X). Never inline secrets."

Layer 2: akb_help(topic="vault-skill", vault?)  (router)
  • vault arg + vault has overview/vault-skill.md → return that doc's body
  • vault arg + missing → return short "missing" notice with akb_put template
  • no vault arg → topic explanation only

Layer 3: overview/vault-skill.md  (the actual contract, in AKB)
  • Normal AKB document (type="skill")
  • Vault owner authors and edits with regular AKB tools (akb_get/akb_edit/akb_put)
  • Seeded on vault create with the standard template
  • Never auto-generated for existing vaults — owner must opt in
```

## Data model

**New doc_type value:** `"skill"`

| Surface | Change |
|---|---|
| `documents.doc_type` (TEXT column, no DB enum) | accept `"skill"` |
| MCP `akb_put` `type` enum | append `"skill"` |
| `app/models/document.py` doc-type literal | append `"skill"` |
| `metadata_worker._DOC_TYPES` | append `"skill"` |
| `documents.metadata` JSON | no new required fields in v1 — `scope` can be inferred from path |

**No new tables, no DB migration, no new edge type, no new index.**

A skill doc looks like any other AKB document; `doc_type="skill"` is the distinguishability marker. The path `overview/vault-skill.md` is convention only.

## Behaviors

### B1. `akb_help` extended

`akb_help(topic="vault-skill", vault?="X")`:

- `vault` not passed → static topic body explaining the convention (what vault-skill is, where it lives, how to author one).
- `vault` passed + `overview/vault-skill.md` exists → return:
  ```markdown
  # Vault skill for {vault}
  <!-- akb-skill-source -->

  Source: vault owner (overview/vault-skill.md, version {commit-or-updated-at})

  {body of the doc, verbatim}
  ```
- `vault` passed + doc absent → return:
  ```markdown
  # Vault skill for {vault}

  No `overview/vault-skill.md` found in this vault.

  The vault owner can create one with:

      akb_put(
        vault="{vault}",
        collection="overview",
        title="Vault Skill",
        type="skill",
        content="<see akb_help(topic='vault-skill') for the template>"
      )

  Until then, follow general AKB conventions:
  - akb_browse before writing to learn the existing collection layout
  - akb_search / akb_grep before writing to avoid duplicates
  - Never inline secrets; use `${{secrets.X}}` placeholders
  ```

The "no-vault" topic body (the second-line bullet list) doubles as the embedded default guidance — short and prescriptive, but **not** templated for materialization into each vault. It lives in `backend/mcp_server/help.py` as a static string.

### B2. `initialize.instructions` (MCP server)

When the MCP server responds to the initialize handshake, set the `instructions` field to ~80 tokens:

```text
AKB stores documents/tables/files in vaults. Before writing into a vault:
1. Call akb_help(topic="vault-skill", vault="{name}") to read the owner's conventions for that vault.
2. If the vault has no vault-skill, follow the fallback guidance in that response.
3. Use akb_browse before akb_put on an unfamiliar collection.
4. Never inline secrets in document bodies — use ${{secrets.X}} placeholders.
5. Destructive tools (akb_delete_vault, akb_delete_collection) require explicit user confirmation.
```

Claude Code injects this into the system prompt every turn (no compaction loss, confirmed via dbreunig's reverse-engineering of CC's prompt assembly). Cursor and Cline implementations vary — graceful degradation: if a host ignores `instructions`, the agent only sees `akb_help` output when they invoke it, which is still strictly better than the current state.

### B3. Vault creation template seed

`backend/app/services/document_service.py:create_vault` seeds `overview/vault-skill.md` for every non-mirror vault **directly** (not inside `_apply_template`), so vaults created with `template=None` also receive the seed. Place the seed call after `_apply_template` returns and inside the existing `try` block so the vault-create rollback covers it.

Template body — short, prose-only, no frontmatter rules in v1:

```markdown
# {Vault} Vault Skill

> Edit this document to describe how agents should write into this vault.
> Until you do, it acts as the AKB-default template — agents fall back to
> general AKB conventions (browse before write, no inline secrets, etc.).

## Purpose

(Describe what this vault is for and what it is not for. One paragraph.)

## Document types

Use these types in akb_put. Skip the rest unless the body explicitly calls for them.

- note — lightweight record
- report — synthesized analysis
- decision — durable decision with rationale
- spec — technical or product specification
- plan — future work
- session — agent session record
- task — assignment
- reference — stable reference material

## Tag conventions

- topic:&lt;slug&gt; — concept grouping
- source:&lt;system&gt; — imported source family
- area:&lt;slug&gt; — organizational area

## Collections

(Optional — list collections and their write policy here. Vault owner can
free-form this section. Agents read it as context, not a hard schema.)

## Relation rules

- depends_on — one resource cannot be understood without another
- references — background citation
- derived_from — generated/curated work depends on source material

## Do not

- Inline secrets in bodies; use ${{secrets.X}} placeholders
- Edit auto-generated docs without checking provenance
```

Existing vaults are not backfilled. If an existing-vault owner wants a skill, they create it manually via `akb_put` (the missing-doc response from B1 gives them the call).

### B4. Authoring / editing

Skill docs are normal AKB documents. The owner edits with `akb_edit` / `akb_update` like any other doc. `akb_browse(vault, collection="overview")` lists it. `akb_search(vault, "skill")` finds it. Git history is normal. Nothing special.

## Tests (E2E shape, not exhaustive)

```bash
# B1 — akb_help router
akb_help(topic="vault-skill") returns the static topic body
akb_help(topic="vault-skill", vault=<v_with_skill>) returns the doc body
akb_help(topic="vault-skill", vault=<v_no_skill>) returns the missing notice + akb_put template

# B2 — initialize.instructions
MCP initialize response includes instructions field with the 5-bullet text
text length is under 1KB

# B3 — vault create seed
akb_create_vault(name=<new>, template=<any>) creates overview/vault-skill.md
the created doc has type="skill"
the body matches the seed template (with {Vault} substituted)
existing vaults are NOT touched by any migration

# B4 — author workflow
owner can akb_edit overview/vault-skill.md
edited body appears on next akb_help(topic="vault-skill", vault=<v>) call
type="skill" is preserved across edits
```

## Implementation plan

| Phase | Change | Files | LOC |
|---|---|---|---|
| **P1** Data model | `"skill"` added to doc-type enum | `tools.py` (line 73, 214), `models/document.py` (line 17), `metadata_worker.py` (line 38) | ~5 |
| **P2** Help router | `akb_help(topic="vault-skill", vault?)` branch + missing notice; **also extend `tools.py` `akb_help` tool schema to declare `vault?: string` parameter** | `mcp_server/help.py`, `mcp_server/server.py` handler, `mcp_server/tools.py` schema | ~60 |
| **P3** Initialize instructions | `Server("akb")` initialize handler returns `instructions` | `mcp_server/server.py` (~line 71), `mcp_server/http_app.py` | ~15 |
| **P4** Vault create seed | After template apply, write `overview/vault-skill.md` | `app/services/document_service.py` (around line 822) | ~30 |
| **P5** E2E | Cover B1–B4 | `backend/tests/test_skill_e2e.sh` (new) | ~80 |
| **Total** | | | **~190 LOC** |

Estimated effort: **3 hours** including tests.

## Out of scope (deferred to v1.1+)

These are intentionally not in v1. Each is additive and requires no data migration if added later.

1. **Collection-level skill** (`{collection}/_skill.md`) — add `collection?` arg to `akb_help(topic="vault-skill", ...)` later, concatenate after vault body.
2. **Frontmatter `rules` block** — agents that want deterministic policy can put `--- rules: { allowed_types: [...] } ---` in their vault-skill body manually in v1; backend doesn't merge yet. v1.1 parses + section-key override.
3. **Graph edges** — `akb_link` between vault-skill and collection-skill (when collection-skill exists) using `relation="references"` with `metadata.role="specializes"`. UI/audit only — never load-bearing for resolution.
4. **Exemplar tagging** — owner marks best-practice docs with `tags=["akb:exemplar"]`; `akb_help` response appends their doc-ids as references.
5. **Dedicated `akb_skill_resolve` tool** — only justified once cascade has ≥2 layers. v1's single layer doesn't need a separate tool.
6. **Backfill for existing vaults** — explicitly not done. If real demand surfaces, a one-shot script can seed missing vaults; until then, owner-initiated creation is the rule.

## Risks

| Risk | Mitigation |
|---|---|
| Hosts other than Claude Code ignore `instructions` | Layer 2 (`akb_help`) is independent — agent invokes it directly. Layer 1 is best-effort. |
| Vault owner never edits the seeded template | The seeded body is itself usable. Worst case = default behavior. |
| `doc_type="skill"` collides with future external use | TEXT column, no SQL enum — collision is at MCP schema layer only. Easy to rename. |
| `_apply_template` rollback paths during vault create | Existing rollback already covers extra files. New seed sits inside the same transaction. |

## Open questions

- **Include `updated_at` / commit-sha in B1 response header?** Decided: **yes**, as the `version {commit-or-updated-at}` line already shown in the B1 template. Tiny cost, useful for agents detecting changes mid-session.
- **Auto-link owner's username in the missing-vault-skill response?** Decided: **no in v1** — keep the response small. Owner can use `akb_vault_info` themselves.

---

## Summary

Three layers, one new doc-type value, one router branch, one initialize change, one template seed. **~190 LOC, no schema migration, no new tool, no new endpoint, no filesystem write to the user's machine.** Everything else from the 2026-05-06 design is deferred until real demand justifies it.
