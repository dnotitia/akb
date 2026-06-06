# LLM content gate for public exposure — Design

**Status**: draft, awaiting review
**Branch**: TBD
**Started**: 2026-06-07

## Statement

When document content becomes reachable by an unauthenticated/public
reader, AKB optionally runs the content through an **LLM scan** that
flags likely-sensitive material (credentials, keys, PII, internal
hostnames/IPs). The scan is a **reusable, pure function**; each call
site decides how to act on its verdict.

**v1 ships in *advisory* mode**: the scan never blocks a write or a
publish. It attaches non-blocking `content_warnings` to the response so
the agent (or the human reviewing the agent's transcript) can
self-correct. Hard blocking and a `force` override are deliberately
**deferred** — see [Deferred](#deferred-not-in-v1).

## Why — threat model

The thing we actually want to prevent is *sensitive content leaving the
trust boundary*. In AKB, content crosses to public-readable through
exactly two mechanisms, and they have **different trust levels**:

| Exposure path | Granularity | Required role | Who does it | Frequency |
|---|---|---|---|---|
| `akb_set_public` | whole vault | **owner only** | human owner, deliberate | rare |
| `akb_publish` | single doc/file/query | **writer** | delegated humans **+ agents** | common |

Source facts (verified 2026-06-07):

- `public_access` is a column on the **`vaults` table only**
  (`backend/app/db/init.sql:58`, enum `none|reader|writer`).
  `collections` and `documents` have **no** public/visibility column.
  There is no document-level or collection-level public flag.
- `akb_set_public` → `access_service.set_public_access()` enforces
  `check_vault_access(..., required_role="owner")`
  (`access_service.py:739`). Owner-only.
- `akb_publish` → handler enforces
  `check_vault_access(..., required_role="writer")`
  (`mcp_server/server.py:923`). Writer minimum (owner/admin/writer).
- `publications` is a separate table (`init.sql:270-310`),
  `resource_type ∈ {document, table_query, file}`, slug-based
  `/p/{slug}` link. **Orthogonal** to `public_access`: it exposes a
  single resource out of an otherwise-private vault.

The highest-risk, highest-frequency path is therefore **a writer-scoped
agent publishing a document that contains a secret.** `set_public` is a
rare, owner-only, deliberate act. This ranking drives the rollout order
below.

### Why not the original "gate at write-time on public vaults"

The first idea was: in `akb_put`/`akb_update`, if the target vault is
public, scan before commit. Problems:

1. **It misses the common path.** A secret written to a *private* vault
   and then exposed via `akb_publish` never sees the gate — at write
   time the vault was private.
2. **It's the first synchronous LLM call on the write path.** Today
   `put`/`update` do zero LLM round-trips on the request thread
   (indexing/embedding/auto-tag are all async workers). Gating writes
   regresses that.

The correct trigger is the **moment of exposure** (`publish` /
`set_public`), not the moment of write. Exposure is where the trust
boundary is actually crossed, the content already lives in git, and
there is no write-path latency to regress.

### Why advisory-first (not block)

Hard-blocking with no override punishes LLM false positives: a public
KB legitimately contains example keys, redacted samples, CVE
write-ups. Without a `force` escape hatch (deferred), a FP would *lock
the user out* with no recourse. Advisory mode:

- zero UX friction — nothing is blocked;
- FP cost is one warning line, never a lockout;
- removes the need for `force` in v1 (nothing to override);
- lets us **measure the real FP rate** before deciding whether to
  harden into a block.

This is a deliberate observe-first rollout. Hardening later is a config
flip + adding `force`, not a rewrite (the gate module is built to
support both from day one).

## Model

### The gate is a cross-cutting concern, not scattered patches

Architecturally identical to `check_vault_access()` — one
implementation, invoked at each boundary. The logic (policy + LLM call +
verdict shaping) lives in **one module**; call sites are one-liners with
**no policy branching**. That is why wiring it into 1–3 places is not
"땜빵 sprawl": it is the same pattern AKB already uses for access
control everywhere.

A purely-async single choke-point is impossible for a *blocking* gate
(content is already in git by the time a worker sees it) — but in
advisory v1 we are not blocking, so the call sites can even be
synchronous-but-cheap (one doc per publish).

### Gate module — `backend/app/services/content_gate.py`

Pure, no enforcement decision inside:

```python
# Verdict is data; the caller decides what to do with it.
@dataclass
class Finding:
    category: str       # api_key | token | password | pii | internal_host | ...
    excerpt: str        # short redacted snippet, e.g. "sk-live-…a91"
    confidence: str      # high | medium | low
    line_hint: int | None

@dataclass
class GateResult:
    findings: list[Finding]
    scanned: bool        # False if skipped (disabled / not configured / empty content)
    degraded: str | None # set if the LLM call failed; advisory mode swallows it

async def scan(content: str, *, ctx: GateContext) -> GateResult: ...
```

- Reuses the existing LLM client `llm_service.chat_json()`
  (`backend/app/services/llm_service.py`) — OpenAI-compatible,
  `json_object` mode, `LLMError`/`LLMPermanentError` already
  differentiated, blank config ⇒ disabled.
- `GateContext` carries `vault_name`, `exposure_reason`
  (`publish|set_public|write`), `resource_uri` — used for prompt
  framing and audit, never for enforcement logic.
- **Never raises in advisory mode.** LLM error ⇒
  `GateResult(scanned=False, degraded="...")`. The caller logs it and
  proceeds. (When `block` mode lands later, the *caller* chooses
  fail-open vs fail-closed on `degraded` — kept out of the pure module.)
- Skip conditions resolved here: gate disabled, LLM unconfigured,
  empty/binary content.

### Policy resolution (keeps "private-skip" a setting, not a hardcode)

The exposure-path → "should scan?" mapping is config-driven so the
default can change later without touching call sites:

```yaml
# config/app.yaml  (new block; opt-in, defaults off — mirrors llm_* pattern)
content_gate:
  enabled: false            # master switch
  enforcement: audit        # audit (v1) | block (future)
  model: ""                 # falls back to llm_model when blank
  timeout: 15
  min_confidence: low       # findings below this are dropped from the response
  scan_on:                  # which exposure paths run the scan
    publish: true           # Phase 1
    set_public: false       # Phase 2
    write_public_vault: false  # Phase 3
```

`enabled: true` while `llm_base_url` is blank is a **startup config
error** (not silent no-op) — fail loud on misconfiguration.

## Phase 1 — wire `akb_publish` (the only v1 call site)

Single choke-point: `publication_service` create path (backed by
`mcp_server/server.py:892-952`, role check at `:923`).

Flow:

1. Resolve the document body for the publication target. For v1 scope
   to `resource_type == "document"` only (skip `file`/`table_query` —
   binary/SQL, lower text-secret risk; revisit later).
2. If `content_gate.enabled` and `scan_on.publish`, call
   `content_gate.scan(body, ctx=publish)`.
3. **Advisory:** attach findings to the publish response; do **not**
   block. Drop findings below `min_confidence`.

Response shape (additive, non-breaking):

```json
{
  "slug": "...",
  "share_url": "/p/...",
  "content_warnings": [
    {"category": "api_key", "excerpt": "sk-live-…a91", "confidence": "high", "line_hint": 42}
  ]
}
```

Empty/absent `content_warnings` when nothing flagged or scan skipped.
No new error code needed in v1 (nothing is denied).

## LLM prompt & false-positive control

- `chat_json` `json_object` mode, `temperature=0`.
- System prompt instructs: flag **only high-confidence live secrets**;
  explicitly **ignore** example/placeholder/redacted values, obvious
  dummies, and `${{secrets.X}}` references (AKB's sanctioned
  placeholder).
- Return per-finding `confidence` so the future `block` mode can
  threshold (e.g. block only `high`).
- Body truncation cap (reuse metadata_worker's ~6000-char convention)
  to bound token cost; note truncation in `degraded` if it triggers.

## Coverage gaps (explicit, by design in v1)

These are **known and intentionally unhandled** in v1 — documented so
they are not mistaken for "covered":

- **`set_public`** (Phase 2): flipping a vault public does not re-scan
  existing docs. Owner-only + rare ⇒ lower priority. When added,
  semantics = *advisory report* (list flagged docs to the owner),
  possibly async due to N-doc fan-out — not a per-doc hard block.
- **write-time on already-public vaults** (Phase 3): writing a secret
  into a vault that is *already* public is only caught on its next
  publish, not at write. `put`/`update`/`edit` in
  `document_service`.
- **`akb_edit`, `file` param, `akb_put_file`** content paths share the
  same gap as write-time; folded into Phase 3.
- The gate is **probabilistic** — it is defense-in-depth / a nudge, not
  a guarantee. It does not replace the `${{secrets.X}}` discipline.

## Roadmap / phases

| Phase | Scope | Enforcement | Notes |
|---|---|---|---|
| **1 (v1)** | `akb_publish` (documents) | advisory | observe FP rate, ship the module |
| 2 | `akb_set_public` | advisory report | N-doc fan-out, owner-facing |
| 3 | write-time (put/update/edit, public vaults) | advisory | closes the residual write gap |
| later | promote to `block` mode | block + `force` | data-driven; threshold on `confidence`; `force` authority decided then (candidate: owner/admin only, with `document.gate_bypassed` audit event) |

## Deferred (not in v1)

- **`force` override** — no escape hatch in v1 because nothing blocks.
  Its authority model (any writer vs owner/admin-only) is re-opened when
  `block` mode is considered.
- **`block` / hard-deny** — gated behind real FP-rate data from advisory
  mode. Will need a new `CONTENT_BLOCKED` error code in
  `backend/app/util/errors.py` (today `ForbiddenError` etc. collapse to
  `code:"internal"` — that mapping should be fixed when we add a clean
  blocking code) and the fail-open/fail-closed decision on LLM
  `degraded`.

## Files touched (v1)

- **new** `backend/app/services/content_gate.py` — pure `scan()`.
- `backend/app/config.py` — `content_gate` settings block + startup
  validation.
- `config/app.yaml.example` — documented `content_gate` block (off).
- `backend/app/services/publication_service.py` (+ publish handler) —
  one call to `content_gate.scan`, attach `content_warnings`.
- (no proxy change, no new MCP param, no new error code in v1)

## Open questions

1. Publish targets `file`/`table_query` — defer scanning these, or
   scan `table_query` SQL/results too?
2. `min_confidence` default — `low` (noisier, better recall for the
   observation window) vs `medium`?
3. Should advisory warnings also emit an event (e.g.
   `publication.content_flagged`) for dashboards / FP-rate measurement,
   or is the response field enough for v1?
