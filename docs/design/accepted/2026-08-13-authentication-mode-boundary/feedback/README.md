# Feedback

Exact-head reviews produced two accepted corrections to the Phase 0 record:

- reconcile it explicitly with the earlier accepted account-governance ADR so
  both records have a clear and limited authority boundary; and
- require OIDC profiles to validate exact issuer, intended AKB
  audience/resource, and accepted credential/token type before projection,
  while requiring fresh installs and ambiguous legacy configuration to fail
  closed instead of defaulting to local authentication.

The reviews also identified the missing `rounds/` and `feedback/` directories
required for design items. This tracked summary closes that topology gap.

Only the resulting public product decisions are recorded here. Detailed review
transcripts and implementation-specific speculation are not part of this ADR.
