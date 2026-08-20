# Final backend review — feat/vault-skill-reservation (2026-08-19)

Whole-branch adversarial review (dc6f893..4d45ebd) after all 10 backend tasks
passed individual spec+quality review. Verdict: **do not ship as-is** — the
reservation guards are clean (no bypass found; see "Guard coverage" below),
but the injection layer trusts an attribution helper designed for analytics,
not authorization. Both blocking findings were reproduced live.

## Blocking

1. **Cross-vault skill disclosure.** `call_tool`'s injection block derives the
   target from raw caller args (`vault_of_call`) and
   `vault_skill_service.injection_payload` performs no authorization.
   `akb_help` accepts a `vault` argument and never calls `check_vault_access`
   — so `akb_help(topic="quickstart", vault=<any vault>)` returns the target
   vault's full skill body to a non-member, and doubles as a vault-existence
   oracle. Reproduced with a two-user setup (control calls correctly returned
   `permission_denied`; the help call leaked the marker). Fix direction: the
   injector must only follow a *completed* access check (authorized-vault
   contextvar set by `check_vault_access`), not re-derive the target from
   args — an injector-side `check_vault_access` call is the fallback.

2. **`_vault_cache` unbounded + attacker-keyed.** Negative entries are
   inserted for any string reaching the injector; keys are never evicted
   (TTL governs freshness only), and vault-name length is not clamped (5000
   chars accepted). Cheap authenticated HTTP loops grow resident memory
   without bound on a replicas=1 API with a 503 history. Fix: LRU bound +
   name clamp; the contextvar fix in (1) also restricts keys to real,
   authorized vaults.

## Non-blocking

3. `akb_help(topic="vault-skill", vault=X)` serves any vault's skill with no
   access check (pre-existing; the design promotes this channel, so close it
   here with a reader-role check).
4. Legacy `akb_edit` on the canonical doc never invalidates the version cache
   (only `update()` does; native edit is fine via `_update_from_snapshot`).
   Reproduced: stale payload after an edit, up to TTL.
5. `collection_service.create` lacks the reserved-namespace guard:
   `akb_create_collection(path="overview/junk")` succeeds and the delete
   guard then makes it permanently undeletable litter.
6. The backfill scans `documents` only — files/tables already under
   `overview/` survive silently; a clean dry run overstates compliance.
7. Injection cache-miss does ~5 sequential DB round trips + a git read on the
   hot path with no timeout and no single-flight; concurrent first-touches
   stampede. (Also `_ensure_document_hash` can write on this read path.)
8. `create_vault` never invalidates the negative cache — a name probed before
   creation suppresses first-touch injection for up to a TTL.

## Verified-as-decided

Mirror-vault injection exclusion airtight (sidecar row inserted in the same
TX as the vault row — no window); archived-backfill exclusion NULL-safe;
error code `permission_denied` confirmed; export keeps skill docs;
`akb_edit` cannot retype or relocate.

## Guard coverage (clean)

All document writes funnel through the two services; REST adds no bypass;
`slugify` strips `/` (no path smuggling via slug); `parent` URI resolves to
the guarded collection; no collection rename exists; `initiate_replace` /
`alter_table` take no collection; `akb_sql` access-checks every listed vault;
`skill_internal` is keyword-only with no `**kwargs` splat anywhere.

## Test gaps

Non-member-never-receives-payload (unit + two-user e2e — the current e2e is
single-owner, so no authorization path is exercised); `akb_edit` invalidation;
`_vault_cache` boundedness; files/tables violation fixtures in the backfill.
