# Rounds

This Phase 0 item went through three public design rounds.

- **Baseline round** — separated permanent authority boundaries from versioned
  verifier profiles and migration-only compatibility, while leaving detailed
  route, claim, key, and UI schemas to their implementation phases.
- **Accepted-record reconciliation** — added a narrow partial-supersession
  boundary with the earlier account-governance decision. Human-auth defaults,
  session issuance, runtime hybrid behavior, and permanent symmetric-session
  compatibility moved to this ADR; durable account and authorization contracts
  stayed with the earlier record.
- **Independent exact-head review** — made exact issuer, intended AKB
  audience/resource, and accepted credential/token type mandatory OIDC profile
  invariants; made missing or unknown install-time mode fail closed; and added
  the design item's required evidence topology.

Each round changed only durable public architecture. Provider-specific claim
layouts, internal implementation names, and migration-window durations remain
deliberately open.
