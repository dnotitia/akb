# AKB License Change — PolyForm NC 1.0 → Business Source License 1.1

**Effective**: starting with the next AKB release after this document
lands on `main`. All prior releases remain under their original
[PolyForm Noncommercial 1.0](https://polyformproject.org/licenses/noncommercial/1.0.0)
terms.

## Summary

AKB is moving from **PolyForm Noncommercial 1.0** (which forbids any
commercial use) to **Business Source License 1.1** with a **100 Named
Seats** Additional Use Grant.

Net effect for users: **the door opens**. Small commercial deployments
that were previously forbidden are now explicitly permitted. The only
case where a commercial license is required is at scale (≥100 seats) or
when offering AKB as a service to third parties.

Each release ships with a 4-year clock — on its Change Date that
specific release converts automatically to **Apache License 2.0**.

## What changes

| | Before (PolyForm NC 1.0) | After (BSL 1.1) |
|---|---|---|
| Non-commercial use | Free | Free |
| Personal / hobby / research | Free | Free |
| Educational / public-research orgs | Free | Free |
| Internal commercial use, <100 seats | **Forbidden** | **Free** |
| Internal commercial use, ≥100 seats | Forbidden | Commercial license required |
| Hosted service to third parties | Forbidden | Commercial license required |
| Modification + redistribution | Free, must stay PolyForm NC | Free, must stay BSL 1.1 (until Change Date) |
| Eventually becomes OSI-approved OSS? | No | **Yes** — Apache 2.0 four years after each release |

## Why we changed

The PolyForm NC was too restrictive in practice. It blocked legitimate
small-team adopters (5-person startups, agency engineers wanting to
self-host) who would never have shown up on our commercial radar
anyway, and it conflated "agent-based knowledge management is mature
enough to charge for" with "any commercial touch is forbidden".

BSL 1.1 lets us keep the protection where it actually matters — at
scale and against commercial hosting / rebranding — while removing
friction for small teams who want to evaluate, deploy internally, and
build on top of AKB.

The 4-year convert-to-Apache-2.0 clause is a long-term commitment: any
version of AKB we ship today is guaranteed to be under a permissive
OSI-approved license within four years, no further action required.
That clause is irreversible — we cannot retract it on a given release
once that release is out.

## The 100 Named Seats line

The Additional Use Grant in the [LICENSE](./LICENSE) is the
load-bearing text. This section explains the intent in plain language.
**If the two ever conflict, the LICENSE wins.**

A **Named Seat** is one distinct user account in your AKB deployment's
`users` table. We count:

- ✅ Human user accounts (interactive login accounts)
- ❌ Service accounts (bots, CI, automation agents)
- ❌ Accounts with no successful login in the trailing 90 days

The count is **per deployment**, not per company. A company running
two separate AKB deployments (e.g. one for engineering, one for
support) with 80 seats each is fine — neither deployment crosses 100.

The same human person counted across multiple deployments is counted
once per deployment, not deduplicated.

### Why "Named Seats" and not MAU or revenue

- **Machine-checkable** — anyone (operator or licensor) can run
  `SELECT COUNT(*) FROM users WHERE ...` and get the answer. No
  arguments about what "active" means.
- **Stable** — doesn't depend on whether your business model is paid
  or free, ad-supported or subscription, profit or non-profit.
- **Hard to game without lying** — sharing one account across many
  humans violates intent; we trust good-faith counting.

### What counts as a separate "deployment"

A deployment is one running AKB stack with its own `users` table —
typically one Postgres database, one bare-repo store, and one set of
backend/frontend pods. Two deployments that share a database are one
deployment.

This is a deliberate choice: it means a fork that runs a single
AKB instance for a large user base needs a commercial license, but
an organization that genuinely runs many small isolated instances is
not penalized.

## Examples

| Scenario | Verdict |
|---|---|
| Solo founder running AKB for personal notes | Free |
| 12-person startup running internal AKB for engineering docs | Free |
| University research lab, 40 grad students | Free |
| 80-person company running AKB across engineering + ops | Free |
| 250-person company running AKB for all employees | **Commercial license required** |
| SaaS provider offering "managed AKB" to clients | **Commercial license required** (regardless of seat count) |
| OSS project running AKB on a community-facing instance | Free if <100 named seats; talk to us if larger |
| Fork called "MyKB" with 500 users | **Commercial license required** + must rename (see [TRADEMARKS.md](./TRADEMARKS.md)) |

## What's NOT covered by BSL 1.1

The npm `akb-mcp` proxy (`packages/akb-mcp-client/`) is and stays
**MIT-licensed**. It's a thin stdio ↔ HTTP forwarder designed to be
embedded inside arbitrary MCP-aware agent clients (Claude Code,
Cursor, Windsurf, custom agents). Keeping it MIT removes any
friction for those embedders — anyone can vendor or redistribute the
proxy without worrying about seat counts or Change Dates.

The BSL 1.1 terms apply to the AKB backend (the actual knowledge
base), the frontend, the deployment manifests, and the rest of the
repo. The 100 Named Seats threshold attaches to a running AKB
backend, not to client installations.

## What stays the same

- **Trademarks** — [TRADEMARKS.md](./TRADEMARKS.md) is unchanged. The
  AKB / Dnotitia / Seahorse names and logos remain trademarks of
  Dnotitia, Inc. and are not licensed via BSL or Apache 2.0.
- **Contributor terms** — `CONTRIBUTING.md` already pre-authorized this
  change in §3 (relicensing). Existing contributors do not need to
  re-sign anything. New contributions will land under BSL 1.1.
- **Past releases** — every version released under PolyForm NC remains
  under PolyForm NC for the copies already out there. We do not
  retroactively relicense old releases.

## Frequently asked

**Is BSL "open source"?** No, not under the OSI definition. It's
"source-available" until the Change Date, then it becomes OSI-approved
Apache 2.0. We follow the same convention as HashiCorp, MariaDB,
Sentry, Couchbase, Materialize.

**Can I modify and redistribute?** Yes — same as before. You must keep
the BSL 1.1 license intact on your modified copy (you cannot relicense
it under MIT, for example, until the original Change Date is reached).

**Can my fork remove the 100-seat cap?** No. The Additional Use Grant
is fixed by the Licensor (Dnotitia). Forks inherit the same terms.

**Will the seat threshold change?** It might in future versions of
AKB. Each released version is locked to the threshold in effect at
release time — we cannot raise the threshold for a release that's
already out (we can only make it more permissive, not stricter).

**What if I cross 100 seats mid-deployment?** Contact us before you
cross. We won't try to ambush you — the intent is to convert organic
growth into a conversation, not to sue.

**Will AKB ever go back to "fully open"?** Yes, automatically, on each
release's Change Date (release date + 4 years). That conversion to
Apache 2.0 is irrevocable.

**Why not just MIT / Apache from day one?** Because once code is
permissively licensed, anyone (including a well-capitalized competitor)
can take it, rebrand it, host it, and undercut the people maintaining
it. The 4-year BSL window funds the maintenance that gets each release
to its Apache 2.0 conversion.

## Contact

Commercial licensing, the rationale here, or trademark permission
requests: **support@dnotitia.com**.
