# Rounds

This decision was made in public on
[#407](https://github.com/dnotitia/akb/issues/407), in four rounds.

- **Proposal round** — stated the defect from source at `6db98bf`, named the two
  independent automated grantors that need the same primitive, and put six
  contract choices with recommendations. Asked for a direction on 4.1 above all,
  because that is where the measured cost differs by an order of magnitude.
- **Scope round** — separated what is cheap now and expensive later (one
  derivation function, the basis and both effective roles on the events, an
  explanation surface) from what looks like a natural extension and is where
  this kind of model usually goes wrong (deny, expiry, a finer grain). Named the
  one real fork — whether a contribution's subject is a user or a set — and what
  measurement would settle it.
- **Decision round** — chose 4.1(A), and opened by withdrawing the argument the
  proposal had made for it. The published cost argument counted read sites,
  which is exactly what the strongest form of (B) preserves; the decision rests
  instead on reversibility, since the contribution table is identical under both
  and only one direction is a cheap later refactor.
- **Correction round** — withdrew a measurement about the absence of a periodic
  reconciler. See `feedback/`.

Implementation followed the accepted direction directly, so no further design
round was needed.
