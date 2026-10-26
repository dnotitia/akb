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
| `/readyz` |Successful DB result cached for 30 seconds; vector health is soft.|
| `/health` |Detailed operational/queue diagnostics, not a cheap liveness check.|

Readiness bounds DB pool initialization plus ping together to 2 seconds. The
vector check retains its 5-second budget. These are probe-step budgets, **not**
a 2-second whole-endpoint guarantee: callers can wait for the shared readiness
lock. Concurrent failed probes can therefore queue. Failed readiness results
are not cache-fresh, allowing an immediate new probe after recovery.

A cached ready 200 can overlap a 503 from a data request during a new DB outage.
Even a fresh `SELECT 1` cannot prove an unrelated data query will succeed. Do not
use successful probes to erase failed data requests or claim zero downtime.
Probe errors use value-free `error:timeout`/`error:unavailable` and corresponding
soft vector degradation labels rather than raw exception representations.

## Reproduction and regression coverage

`backend/tests/test_transient_api_unit.py` pins REST/MCP errors, sanitization,
no write replay, SQL-enrichment bypass, false-zero prevention, readiness TTL,
soft vector failure, recovery and the DB initialization deadline.

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
