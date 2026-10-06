# Live acceptance drivers

Run only against an explicitly selected endpoint with permission to create and
remove a disposable fixture. Provider sources and existing Vault contents are
not fixtures. Set credentials in the process environment; do not put them in
command arguments or captured output.

## Native change events

`native_change_events.py` exercises REST create, body/status update, obsolete
Head rejection, collection move, unarchive and delete. It reads the operator
Redis stream using XREAD, checks canonical identity and both move scopes, then
removes its own Vault. It never writes Redis. The internal Native restore hook
is covered by the PostgreSQL service tests; there is no public document restore
endpoint in this driver.

Required environment: `AKB_URL`, `AKB_PAT`, `AKB_REDIS_URL`. Optional:
`AKB_REDIS_STREAM` (default `akb:events`). Run with the backend's locked runtime:

```bash
uv run --locked --project backend python scripts/e2e/native_change_events.py
```

The output is safe JSON evidence with committed event metadata and cleanup
status. A failed assertion or HTTP operation exits unsuccessfully. Redis direct
access is privileged: this driver is an operator check, not a browser client.
