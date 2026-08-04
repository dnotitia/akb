# Rounds

This item went through two design rounds before implementation and one
correction round during it.

- **Round 1** — scoped the fix. Established that cleanup had to be reachable
  from every delete path rather than added to the two paths that omitted it,
  because the paths that already had it each carried their own inline copy and
  there was no single original to converge on.
- **Round 2** — adjudicated how far the fix should reach. Concluded that closing
  the delete paths is prospective only, and that a write onto a path a
  publication still claims has to be refused as well. Also concluded that
  binding publications to an identity rather than a path is the structural fix,
  and deferred it to a follow-up rather than folding a schema change into the
  containment work.
- **Correction round** — three defects found while implementing, each verified
  in the tree rather than inferred. They are summarised in the README's
  "What changed" section; the ordering constraint one of them produced is
  recorded as a docstring on the helper it applies to, since that is where it
  needs to be read.

The round notes themselves, the review transcripts, and the production
inventory that informed the remediation ordering are held in the team's
internal notes rather than in this repository.
