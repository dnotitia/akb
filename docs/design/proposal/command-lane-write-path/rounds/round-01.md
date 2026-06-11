# Round 01 — 2026-06-10 — Initial shaping session

Participants: PM (Kyungtae Song), Claude (analysis & drafting).

How the proposal converged, in discussion order:

1. **Git usage audit.** Confirmed bare-repo-per-vault + persistent
   linked worktree + per-vault threading.Lock + per-(vault,path)
   advisory lock. Evaluated and rejected worktree-per-write + merge
   (no content-conflict class exists; only ref contention).
2. **Load target introduced:** ~1000 concurrent agents, hot case
   ~80 writes/s on a single document. Measured the git commit sequence
   at 73 ms (Appendix A) → derived the ~10–25 writes/s serial ceiling
   and, critically, the **pool-poisoning cascade** (lock waiters pin
   pool connections; max_size=20; statement_timeout=30s mass-cancel).
   PM floated switching to optimistic concurrency; rejected for the
   API path (retry storms under contention) — the bottleneck is git
   inside the critical section, not the locking flavor.
3. **EDA discussion.** Established the current system is a synchronous
   core + transactional outboxes + polling workers, not EDA — and that
   observability and future internal agents (gardener/collector) need
   a complete event backbone + level-triggered reconcilers, not a
   pub/sub core.
4. **PM clarified intended architecture:** not pub/sub — requests
   wrapped as events, serialized processing, audited. Identified as
   the command-log / actor-mailbox pattern. Adopted with conditions:
   per-(vault,path) partitioning (not global), awaited futures
   (sync façade), bounded queues, no full event sourcing.
5. **PM proposed fire-and-forget** via MCP description change with
   queue-depth/retry-after hints, plus per-pod service_id routing and
   a queue-orchestrator service (with explicit permission to reject if
   overkill). Adopted fire-and-forget as **overflow-only** with durable
   acceptance; rejected service_id routing + orchestrator (pod-topology
   coupling; RWO PVC makes git single-writer anyway) — recorded in
   Alternatives.
6. **Step-by-step rebuild** of the problem (three stacked failures
   A/B/C) and the matched solutions; clarified that serialization
   granularity is already per-document — the defect is the waiting
   room being the global pool.
7. **Atomicity check (PM question):** documented that git-first /
   PG-second already has an unguaranteed crash window today (git ahead,
   stray commit); write-behind reverses the direction into an explicit
   "PG leads, git converges via outbox-as-WAL" invariant. The real
   migration cost is the current-body read path moving to PG, not
   atomicity.

Output: README.md v1 (this folder). Open decisions #1–#5 await PM.
