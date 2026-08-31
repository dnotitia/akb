# Decision rounds

## 2026-08-31 — Path on the API port versus a second listener

A `/stats` path on the existing API port was the smaller change, and it was
rejected because it puts the boundary in the wrong layer. The platform has to
promise that its monitor can read a tenant's counters and cannot read the
tenant's documents; the only mechanism that can enforce that from outside the
workload is a NetworkPolicy, and a NetworkPolicy selects on port. On one port
the promise reduces to a line of application code inside the workload being
constrained.

Accepting a second port then settles the authentication question rather than
raising it. Reachability becomes the authorization, so the surface carries no
credential to distribute or rotate — sound only because the payload is
aggregate counters and because the platform blocks a deployment whose policy is
missing.

## 2026-08-31 — Metrics exporter versus plain JSON

A Prometheus exporter was rejected on two counts: it adds a metrics library to
the tenant image for a single consumer, and read-on-scrape is the wrong shape
here — a duplicated or misconfigured poller would turn `pg_database_size` and
several `COUNT(*)`s into steady load on the serving pool. Sampling on a timer
and serving the cache fixes the cost at one sample per interval regardless of
how often anyone asks, and makes 503-before-first-sample expressible.

## 2026-08-31 — Where the previous day's activity lives

Holding the folded window in process memory was rejected. The consumer keeps
the first value it observes for a window; recomputing after a restart could
answer differently for the same window (retention purge, a late queue flush,
tracking toggled) and silently contradict a series already stored. The window
is therefore persisted with the day as PRIMARY KEY and inserted `ON CONFLICT DO
NOTHING`, so the first writer to close a day decides it permanently.

That permanence is also why the count columns are nullable. A day closed while
usage tracking was off has unknown volume, not zero volume, and writing 0 would
publish a fabricated fact that the design forbids anyone from correcting.

Backfilling days missed during a long outage was considered and rejected: each
tick folds only yesterday, so a window nobody observed stays absent rather than
being reconstructed from whatever raw rows survived. The platform loader treats
a missing row past D+2 as a gap.

## 2026-08-31 — Uvicorn signal capture

`uvicorn.Server.serve()` installs its own SIGINT/SIGTERM handlers for the
duration of the call. A second server started inside the first one's lifespan
would replace the API server's handlers with its own, so a SIGTERM from
Kubernetes would ask the stats listener to shut down while the API server never
learned it was terminating — the pod would be SIGKILLed at the end of its grace
period with in-flight requests and an undrained audit queue. The listener
subclasses `Server` with `capture_signals` as a no-op and is stopped explicitly
from the API's own lifespan instead. Pinned by test.

## 2026-08-31 — Deriving `distilled_doc_count` from the vault label

Counting documents in vaults whose `vault_write_policy.managed_by` begins with
`gardener:` would have produced a number immediately, and was rejected. The
label is free text set at provisioning, so it is a naming convention rather
than a fact the database enforces; the count would include whatever a person
wrote by hand in such a vault, and re-pointing a vault at a different owner
would reclassify its entire history retroactively.

The accepted direction is an explicit marker written by the distillation path —
a fact about the document, which cannot move under it. That change spans this
repository and the gardener, so the field is omitted until it lands.
`sampler._distilled_doc_count` is the only thing here that will change.

## 2026-08-31 — Where the stats socket is opened

The first implementation created the serving task and let uvicorn bind inside
it. Review rejected that: a bind failure — a port rendered onto one already in
use is the realistic case — left `start()` returning True and the boot
continuing, producing a pod that passes every probe with no stats socket on it.
The platform would meet that only as connection errors on its poller, and it is
the same failure `configured_port` already refuses to allow for a malformed
value.

The socket is now opened synchronously with `Config.bind_socket()` and handed
to `serve(sockets=[...])`. uvicorn reports a bind failure by calling
`sys.exit(1)` rather than raising, so the `SystemExit` is converted to an error
naming the address — otherwise it would pass every `except Exception` between
there and the top of the boot and end the process with nothing but a stray log
line.

Checked while making the change: uvicorn does close the sockets it is handed,
from the graceful shutdown it reaches after it has started serving; on the
cancel path the `asyncio.Server` wrapping the fd closes it when collected.
`stop()` closes it anyway, so the release is timed by the stop rather than by
the collector. The test pins the property — the port is free once `stop()`
returns — and not that line specifically; removing it still passes today.
