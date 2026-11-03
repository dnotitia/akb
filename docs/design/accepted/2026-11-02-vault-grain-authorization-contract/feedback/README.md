# Feedback

Writing this record down against the code turned up three corrections.

**ADR-0826 still said the subject was undecided.** Its §8 ended with the
subject question (user or set) "deliberately undecided". On 2026-08-27, about
two months before this record, the question was decided in public on #407:
user subject, no native group. That comment also points to a separate authorization document
that does not exist in this repository. ADR-0826 now carries a dated amendment
that links the public comment and does not cite the missing document.

**ADR-0826 described grant, revoke and transfer as best-effort.** §4.1, §4.4
and §7 described the behaviour measured at `6db98bf`: the catalog commits, and
a best-effort hook converges PostgreSQL afterwards. Since `d6ed12b` (#696),
these three operations project PostgreSQL membership inside the catalog
transaction and abort if the projection fails. The repository `AGENTS.md`
sentence "Hooks are best-effort" was stale in the same way. Both now say what
the code does for these three operations, and nothing about any other
lifecycle path. The correction is backed by reading the code and by the
PostgreSQL suites in §11, not by inferring from the code's structure alone.

**A conformance suite errors at this head for a reason unrelated to
authorization.** `test_access_contributions_postgres.py` pins the expected
last migration to `113_bm25_vocab_epoch.py`, but the registry now ends later.
All 23 tests therefore error in setup before any assertion runs. With the pin
rebound outside the repository, all 23 pass. §11 records this so nobody reads
a red setup as a red gate, or the passing run as a run of the unmodified file.
Fixing the pin is a test change and is left to a separate pull request.
