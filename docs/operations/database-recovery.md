# Database faults, cancellation and recovery

Dependency failures are not invalid SQL, successful empty results, or proof
that a write did not commit. This contract applies to expected errors that
escape the API/MCP operation; search still has its existing partial-result
and degraded-search contract.

## Error contract

| Failure | REST | Stable code |
| --- | --- | --- |
| Connection loss, database shutdown/recovery, exhausted connections/memory |503|`service_unavailable`|
| Driver deadline or PostgreSQL cancellation (`57014`) |503|`operation_timeout`|
| SQL syntax, ACL, constraints, ordinary programming errors |Unchanged existing contract|Not reclassified as a transient fault|

Both codes use the existing error envelope. MCP returns the same stable code
and sanitized message/hint, not an HTTP status. SQLSTATE `57014` can also mean
administrative cancellation, so `operation_timeout` does not claim every such
case was a deadline. A generic network connection/timeout failure outside SQL
also receives the dependency contract; the message does not expose a host,
connection string, SQL statement or raw exception.

The classifier is an explicit SQLSTATE/type list. It does not hide arbitrary
filesystem errors, protocol/authentication/configuration errors, constraint
failures or an asyncpg `InterfaceError` programming mistake. asyncpg 0.31 can
replace connection loss with a closed-transaction `InterfaceError` during
cleanup; only an immediately chained, typed transient cause is preserved.

`count_rows` retains its documented missing-physical-table fallback. Other
failures now propagate instead of pretending the Table contains zero rows.
The user-SQL service does not acquire another connection for SQL-name hints
after a transient failure. Optional post-commit metadata indexing remains
best-effort: its failure must not claim the committed DDL was rolled back.

### Retry policy

There is **no automatic server retry**, for reads or writes. A transient
GET/HEAD response carries `Retry-After: 1`. A client can wait and retry a safe
read with its own small attempt/deadline budget, then present the failure.
Do not run an unbounded retry loop or count a retry as the original request's
success. The header is a backoff hint, not a recovery guarantee.

Writes receive no new retry header. After a transport failure, disconnect or
timeout, read the committed state first and use the operation's existing
CAS/idempotency contract before retrying. A failure response is not a blanket
"no partial write" guarantee. In particular, SELECT/WITH can invoke mutations;
their first keyword is not a safe retry/cancellation classifier.

## Abandoned read requests

For an empty-body GET, these explicit paths cancel their asynchronous request
task after the ASGI server reports `http.disconnect`:

- `/api/v1/documents/{vault}/{document_path}`
- `/api/v1/recent`
- `/api/v1/tables/{vault}`
- `/api/v1/tables/{vault}/{table}/rows`

The middleware consumes the underlying receive stream in one place and relays
it to the application. A GET with a body is passed through without buffering
or monitoring. Writes, raw-SQL POSTs, SSO callbacks, SSE and MCP transports are
not added to this allowlist. Their existing command/protocol deadlines remain.

Cancellation is followed by awaited transaction rollback/pool release, not a
detached `task.cancel()`. A subsequent outer/shutdown cancellation cannot send
a second cancellation through that cleanup; it is re-raised after draining.
The former header-only no-store middleware is pure ASGI now, avoiding its
extra task group's cancellation scope while preserving cache headers and
duplicate cookies.

This is not a promise that every dependency stops instantly. PostgreSQL
cancellation needs a protocol round trip; synchronous/thread-based work cannot
necessarily be interrupted by cancelling its awaiting coroutine. No new
global request timeout, pool timeout, query-plan setting or memory allocation
is introduced. Existing DB command and statement deadlines remain 30 seconds.

## Probes are different from data requests

| Endpoint | Meaning |
| --- | --- |
| `/livez` |Process is serving; no dependency access.|
| `/readyz` |DB ping on a dedicated connection. The request pool is observed, and readiness takes one of its connections only when the pool shows nothing that proves it could serve a request (no idle connection, room to open one, and no recent acquire by a request or background work), or when the server refuses the dedicated connection. Every result is shared by all callers for 1 second, and a plain success is cached for 30 seconds; vector health is soft.|
| `/health` |Detailed operational/queue diagnostics, not a cheap liveness check.|

Readiness bounds DB pool initialization plus ping together to 2 seconds; when
cancelled, nothing in that step waits on the server. The soft vector check runs
as a task that the probe waits on for at most 5 seconds. If it has not finished
by then, it is reported as `vector_store: degraded:timeout`, cancelled, and
left to finish its cleanup in the background, because asyncpg can wait without
limit for the answer to a cancel request. At most one vector check is
outstanding: while a check that was given up on is still finishing, later
probes report `degraded:timeout` at once instead of starting another. Such a
check whose cleanup never finishes keeps that soft label until the process
restarts; readiness itself is unaffected. One evaluation therefore ends within
about 7 seconds, inside kubelet's 8-second probe timeout. Callers can wait for
the shared readiness lock, but they wait for the evaluation already running,
not for one each. Every
result, ready or not, answers all callers for one second, and callers that
arrive while an evaluation runs reuse its result. `/readyz` needs no
authentication, so this bounds the database work that any number of callers
can cause to about one evaluation per second per process. After that second, a
failed result is evaluated again on the next call, so recovery shows within
about a second.

The ping runs on one dedicated connection, not on a request-pool connection.
Readiness takes a pool connection in two cases only, both described below: a
pool that shows nothing that proves it could serve a request, and a server at
`max_connections` that refuses the dedicated connection. Its
`application_name` is `akb-readiness-probe`. The first probe opens it and
later probes reuse it. A failed ping discards it and the next probe opens a new
one; a probe that finds it closed by the server opens a new one before pinging.
Connecting and the ping both stay inside the 2-second step. Each API process
therefore holds one PostgreSQL connection beyond its pool. An unreachable
database still fails readiness at once.

That connection proves the database answers, not that the pool can still open
a connection to it. The pool's idle connections can expire, and a server at
`max_connections` refuses new ones while a connection opened earlier keeps
working. So when the pool shows nothing that proves it could serve a request
(no idle connection, room to open one, and no acquire within
`readyz_pool_saturation_grace_secs`), the probe also takes one connection
through the pool within the rest of its 2-second step and runs `SELECT 1`. If
the pool cannot open it, readiness reports `db: error:pool_unavailable`, or
`db: error:pool_timeout` when it does not answer in time. While the probe waits
for that connection, an acquire completed by a request or background work
proves the pool too and ends the wait, so a probe queued behind a request whose
connection is still opening does not report a working pool as broken.

A server at `max_connections` refuses that connection with SQLSTATE `53300`
while the pool's open connections keep working. Readiness then asks the pool,
without waiting for a connection:

- If the pool has an idle connection, the probe borrows it for the rest of its
  2-second step and runs `SELECT 1` on it: `db: ok:pool_idle`. If that ping
  fails, readiness fails. The connection goes back within the same budget. One
  that cannot finish in time is terminated rather than returned, because
  returning it would wait on a cancel request that may never complete.
- Otherwise, if an acquire has completed within
  `readyz_pool_saturation_grace_secs`: `db: ok:pool_progress`.

Neither result is cached beyond the one-second floor. A closing pool, a pool
with neither, and every other
connection error fail readiness as before.

### Pool saturation

The request pool is saturated when every connection it may open is open and
checked out (`size == max` and `idle == 0`). Requests then wait for a
connection, but the database is not down. A client burst saturates every
replica's pool at the same moment. If readiness failed on saturation alone,
every replica would leave the Service together and the burst would become a
complete outage.

Each probe reads the pool's occupancy without waiting for a connection and
reports it in `detail.pool` whenever it obtained the pool, including when the
database check fails:

| Field | Meaning |
| --- | --- |
| `size`, `max`, `idle` |Open connections, the pool's limit, and open connections not checked out.|
| `free` |Earlier name of `idle`, kept for existing readers.|
| `saturated_for_secs` |Time since the first of the consecutive probes that found the pool saturated; `null` when the last probe found room.|
| `last_acquire_age_secs` |Seconds since the latest completed acquire by a request or background work; `null` before the first. Readiness's own acquires do not count.|

The payload rounds these values to milliseconds; readiness decides on the
values as measured. The count of completed acquires stays internal, because
`/readyz` is public.

A saturated pool whose connections keep changing hands is busy, not wedged,
and stays ready however long it is saturated. Readiness fails only when the
pool has been saturated at every probe for
`readyz_pool_saturation_grace_secs` (default 30 seconds) **and** no acquire
has completed within that time. It then reports not ready with `db: ok` and
the pool figures. That is the leak or stall case, where taking the replica out
of the Service is wanted. The pool counts completed acquires in its asyncpg
`setup` hook, which runs on every acquire. Acquires that readiness makes
itself, for its pool checks and its vector check, are not counted: a check
that got a connection and then failed must not let the next probe skip
checking the pool. A probe that finds an idle
connection, or room to open one, ends the saturation run. While the pool is
saturated:

- A ready result is shared only for the one-second floor, so each probe after
  that looks at the pool again.
- The soft vector check is skipped and reported as
  `vector_store: skipped:pool_saturated`. The pgvector driver on the main
  database borrows from the same pool, so the check would wait for its whole
  budget without changing the verdict.

Probes see the pool's occupancy only when they run, so a pool that had room
between two saturated probes still counts as saturated throughout; the acquire
count is exact. With the shipped probe settings (every 15 seconds, three
failures), a replica whose pool stays wedged leaves the Service about
`grace + 30` seconds after the first probe that sees the saturation. A grace of
0 fails readiness at the first saturated probe and brings back the outage
described above; it is not recommended.

A cached ready 200 can overlap a 503 from a data request during a new DB outage.
Even a fresh `SELECT 1` cannot prove an unrelated data query will succeed. Do not
use successful probes to erase failed data requests or claim zero downtime.
Probe errors use value-free `error:timeout`/`error:unavailable` and corresponding
soft vector degradation labels rather than raw exception representations.

## Reproduction and regression coverage

`backend/tests/test_transient_api_unit.py` pins REST/MCP errors, sanitization,
no write replay, SQL-enrichment bypass, false-zero prevention, readiness TTL,
soft vector failure, recovery and the DB initialization deadline.

`backend/tests/test_readiness_probe_unit.py` pins the busy and wedged verdicts,
the `max_connections` rule, the pool fields, the cache rule, the acquire
counter and its hook, the dedicated connection's settings and replacement, and
the shutdown order. `backend/tests/test_readiness_probe_postgres.py` saturates
a real pool built by `get_pool`. It checks that `/readyz` stays 200 within the
probe budget, and past the grace while more tasks than connections keep taking
turns. It turns 503 once the holders never let go. A role at its connection
limit makes the server refuse the probe connection. That stays 200 through an
idle pool connection, then through recent acquires while every connection is
held, and turns 503 once those stop. The suite also checks that readiness fails at once when the
database is gone, that a terminated probe connection is reopened, and that
shutdown closes it. It reads the same `AKB_TEST_DSN` and `REQUIRE_REAL_PG`
variables as the suite below.

`backend/tests/test_read_disconnect_unit.py` covers the actual main route's
awaited cleanup, normal/error task termination, exclusions, unusual GET bodies,
shutdown during cleanup, cache-header boundaries and duplicate cookies.

`backend/tests/test_transient_recovery_postgres.py` creates a disposable database
and role on an explicitly supplied test-cluster DSN. It tests real command and
statement deadlines, external connection termination, actual running-query
cancellation, rollback, role reset, pool reuse, and a disconnected raw-SQL
write that completes exactly once. Run against a dedicated test cluster:

```sh
AKB_TEST_DSN="${TEST_DATABASE_DSN:?Set a test-cluster DSN first}" \
  REQUIRE_REAL_PG=1 \
  pytest backend/tests/test_transient_recovery_postgres.py -q
```

The CI live-PostgreSQL job explicitly runs this suite; the no-DB unit lane may
skip it. Do not set this variable to production. The tests create/drop their
own randomly named databases and roles and require a test-cluster administrator.

For deployment recovery, capture canonical content hashes/revisions and ACLs,
run separate fixed-arrival data/probe lanes, perform a planned DB restart on
an isolated installation, then compare committed state and queue convergence.
Record HTTP failures, client timeouts and skipped generator starts separately.
Restore the original replica count even if the controller fails. A failed read
alone proves neither data loss nor a failed write, and a restart is not a
physical-cold benchmark unless OS/storage caches were also controlled.

### Isolated deployment observations

Two API replicas were exercised with the existing synthetic-scale data and
unchanged resource/query budgets. A real locked read was observed in PostgreSQL
before disconnecting the HTTP client; the next controller observation found
it gone within 1.221 seconds, including remote observation overhead. Independent
row and catalog queries under an intentional Table lock returned sanitized
`503 / operation_timeout` after 30.092 / 30.109 seconds. Releasing the lock and
performing one explicit delayed read retry returned the unchanged row.

A separate 60-second planned PostgreSQL replacement sampled six lanes, each
with 60 scheduled starts, a five-second client deadline and one active request
per lane. Occupied slots were skipped, not counted as HTTP failures:

| Lane |HTTP200|HTTP503|Client timeout|Skipped start|
| --- | ---: | ---: | ---: | ---: |
|Document/recent/row reads combined|141|0|6|33|
|Liveness|60|0|0|0|
|Readiness|53|2|0|5|
|Detailed health|17|0|4|39|

The replica count was restored in the controller's finally path. Eight
canonical document hashes/heads matched after recovery; the canary's CAS
update/stale rejection and Reader/Outsider checks passed. Indexing/delete and
Native projection queues converged. These are recorded outage outcomes, not
zero downtime, a throughput comparison, or proof that all failed requests
were returned as 503 before the client deadline. Detailed health and operations
outside the explicit disconnect allowlist can still outlive that client.

## References

- [PostgreSQL 16 SQLSTATE catalog](https://www.postgresql.org/docs/16/errcodes-appendix.html)
- [asyncpg 0.31 pool cancellation/reset behavior](https://github.com/MagicStack/asyncpg/blob/v0.31.0/asyncpg/pool.py)
- [ASGI HTTP disconnect semantics](https://asgi.readthedocs.io/en/stable/specs/www.html#disconnect-receive-event)
- [HTTP 503 and Retry-After semantics](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.6.4)

503 denotes temporary unavailability; 504 specifically describes a gateway's
upstream timeout. AKB therefore uses 503 with a distinct timeout code here,
not a misleading claim that it is acting as an HTTP gateway to PostgreSQL.
