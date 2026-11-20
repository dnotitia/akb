# MCP monitoring

Open `/admin/mcp-monitoring` and sign in with your dedicated monitoring account. Select a user in **Users** to see their SSO calls and each PAT used during the selected period. Select a PAT and a tool to see their intersection. Refresh advances the end of the reporting window.

## Grant and revoke access

Provision a separate non-administrator human account with no API keys. In local mode, register the account with a password. In SSO mode, bind the account to its exact identity in the installation's identity provider before granting access.

From the separate product-administrator session, send `PUT /api/v1/admin/mcp-monitoring/access/{user_id}` with `{"enabled":true}`. Use the local admin bearer or the SSO admin cookie and its session-bound CSRF proof. The account becomes dedicated to monitoring. Use a newly provisioned account if you need to replace a local monitor's password.

Set `enabled` to `false` to revoke the grant and end its open monitoring sessions. The account stays dedicated after revocation. Grant access again to let the same account start a fresh session.

Both sign-in modes issue a separate HttpOnly cookie with a hard expiry of at most 30 minutes. The report, operation details, and trace detail accept this session. The browser's product administration and workspace sessions have their own sign-in paths. Sign out from MCP monitoring to end its browser session.

Use **Caller type → Service callers** to see service-account calls and calls authenticated with a service-class key. The default **Users** view covers human SSO and PAT calls. AKB classifies each request by its authenticated account and credential at call time. The selector applies to every breakdown, trend, recent call and operation detail, including retained observations from before this view was introduced.

An MCP application can declare its client name and put its job ID in `params._meta["gen_ai.conversation.id"]` on each tool call. In the service view, **Operations** shows one **Job** per actor, application and job ID. Expand the row for the individual tools, outcomes and timings. Existing clients can send this metadata over stdio or Streamable HTTP.

## Rollout and coverage

Ship the backend and frontend together. The backend installs the monitoring schema at startup. Grant access after every API replica and the frontend run the new version. Suspend dedicated accounts before rolling back to a version that predates this feature. Once every serving replica runs the new backend, both direct Streamable HTTP and existing stdio clients contribute their forwarded `tools/call` requests. History starts when the collectors start.

One observation corresponds to one authenticated tool request reaching the server MCP dispatcher, including a refusal returned with `isError`. Each independently processed client retry counts. The dashboard groups human calls by the verified AKB user ID. A revoked PAT keeps its historical row until the observations expire. The collector uses the internal PAT ID to match a call to its monitoring reference. Local-only proxy activity, authentication failures, initialization, discovery, tool listing and service credentials are outside the human tool-call totals.

The default settings in `config/app.yaml` are shown below.

```yaml
mcp_monitoring:
  enabled: true
  retention_days: 30
  export_traces: false
```

Each API process batches completions asynchronously. Replayed batches use the same observation IDs. A full queue drops new observations, and abrupt process termination can lose buffered events. Inspect the last flush, overdue heartbeats and retained writers' cumulative drops next to the report. An uncleanly stopped writer leaves subsequent coverage uncertain while its record is retained. A partially measured interval shows the observed count. A fully measured idle interval shows zero. An interval without observations or confirmed coverage shows a dash. Disabling collection stops new observations and keeps retention cleanup active.

## Monitoring API

`GET /api/v1/admin/mcp-monitoring` requires a monitoring session. Pass timezone-aware ISO 8601 timestamps for `from` (inclusive) and `to` (exclusive). The default window is seven days and the maximum is 30 days. Set `caller_kind` to `human` (default) or `service`. Pass the same value on the operation detail endpoint. Filter by `user_id`, `auth_kind` (`oauth`, `pat` or `service`) and `tool`. Add the report's `pat_ref` with its `user_id` and `auth_kind=pat`. Each of `user_page`, `credential_page` and `tool_page` is zero-based, with 50 rows per page and continuation flags in `has_more`.

For a selected user, **Credentials** shows the current PAT status and its expiry date when set. The lookup uses the report snapshot even for a historical window. An elapsed expiry marks the PAT expired, a suspended owner blocks an unexpired PAT, and a missing token row is shown as deleted. A historical call keeps its original outcome. **Last observed** is the latest authenticated tool completion matching the selection, including tool errors. OAuth rows show **Session unverified** because the call record does not carry a live identity-provider session. Refresh to check current PAT status again.

**Recent calls** shows the latest 50 matching completions with their time, user, credential, tool, outcome and server duration. Each PAT has its own installation-scoped reference and label, such as **PAT 1**. The same reference and label persist after revocation for retained calls.

The summary, recent calls, trend and breakdowns share one database snapshot. Error rate uses tool and RPC errors divided by decided outcomes. Duration p95 covers all matching completions, including cancellations. The collector gets the actor and credential from authentication. The report exposes the user identity, monitoring reference, label, tool, and timing. PAT management identifiers, names, prefixes, hashes, and bearer values stay outside the response.

## Optional distributed traces

Set `export_traces: true` and configure the API process's `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` to the collector's full OTLP/HTTP traces URL (usually ending in `/v1/traces`). The standard OpenTelemetry sampler and `OTEL_SDK_DISABLED` settings apply to trace export. The database dashboard collects independently of tracing and sampling.

The HTTP middleware covers the MCP mount, including authentication rejections. The Python MCP SDK supplies the protocol span. The AKB exporter keeps the trace IDs, timing, status code and a bounded set of method, tool, authentication and outcome attributes. It discards raw span names, request IDs, headers, events, links and status descriptions before export.

Set `mcp_monitoring.tempo_query_url` to the installation's trusted Tempo query origin to enable **View trace** for sampled calls. The API returns the timing of the matching MCP server span and its HTTP parent when available. The dashboard identifies the call by its observation reference. Set `mcp_monitoring.tempo_retention_hours` to the effective retention for that Tempo tenant and update it when the store policy changes. PostgreSQL call retention is configured separately with `retention_days`.

The API checks Tempo first. If the span is missing and the call is past the configured Tempo retention, the dialog shows **Trace expired**. During the first two minutes after a call completes, a missing span shows **Awaiting trace export** with a retry action. After that grace period a missing span shows **Trace not found** until the configured retention elapses. Leaving Tempo retention unset disables the expiry classification. Connection failures and store errors are reported separately. Historical and unsampled observations remain in Recent calls until PostgreSQL retention elapses.

**Operations** shows one **Conversation** row for calls with the same verified AKB user, agent and conversation identifier, across prompts and credentials. The row shows the agent and a scoped operation ID. Expand it to inspect calls from the selected period. Each expanded row shows the start order, start times, durations, outcomes and individual **View trace** actions. Each action uses the observation’s own sampled span to query Tempo. Elapsed time runs from the earliest start to the latest completion among calls matching the window and filters. It includes gaps and counts overlapping time once. The operation list and call sequence each page through 50 rows.

Conversation grouping uses the identifiers already present in MCP request metadata. Codex supplies `x-codex-turn-metadata.thread_id` and OpenCode supplies `ai.opencode/sessionID`. AKB also recognizes a declared Codex client’s `threadId`. For the AKB extension below, or an explicit `_meta["gen_ai.conversation.id"]`, the declared MCP client name identifies the agent. The collector uses the agent name in the derived, user-scoped grouping key. The report displays fixed labels for known agents and **MCP client** for other agents. Existing rows retain their original groups because the original conversation identifiers were not stored.

When conversation context is unavailable, a valid MCP `_meta.traceparent` takes precedence, followed by a valid HTTP `traceparent` header. Any agent can propagate a W3C context with one trace ID per prompt. Alternatively, an agent can include this AKB extension on each `tools/call` request over stdio or Streamable HTTP:

```json
{"_meta":{"com.dnotitia.akb/turn":{"thread_id":"conversation-8bc5","turn_id":"prompt-2"}}}
```

Place `_meta` in `params`, alongside `name` and `arguments`. Generate a globally unique thread identifier per conversation and a fresh turn identifier per user prompt. Reuse the pair for all tool calls within that prompt, including concurrent calls. Each ID is 1–128 ASCII characters, starts with a letter or digit, and may also contain `.`, `_`, `:`, `@`, `/` and `-`. Client integrations should use identifiers from their agent runtime. Codex CLI already sends its native `x-codex-turn-metadata` object with these two fields. AKB checks the common extension first, then the Codex form, and stores a derived key scoped in reports to the authenticated user. A turn can contain independent Tempo traces or unsampled calls. Raw thread and turn IDs and prompt contents are never needed in the monitoring database.

Requests with only a transport session, login session or per-call ID remain individual. Agents that omit a prompt boundary need their MCP integration to propagate one of the supported contexts. For older records without a start timestamp, shared sampled trace IDs can still recover a trace group and start times are estimated from completion time and duration.

### Agent integrations

With Pydantic AI, set `process_tool_call` on the AKB `MCPToolset`. The run context supplies both IDs when each user prompt starts a new agent run in the same conversation.

```python
async def attach_turn(ctx, call_tool, name, args):
    turn = {"thread_id": ctx.conversation_id, "turn_id": ctx.run_id}
    return await call_tool(name, args, metadata={"com.dnotitia.akb/turn": turn})
```

Claude Code sends a per-call `claudecode/toolUseId`. Its `PreToolUse` hook also receives the session and prompt IDs. For stdio, copy [the optional Python 3 adapter](../../scripts/integrations/claude_mcp_turn.py) to the client and use its absolute path in both settings. For an MCP server named `akb`, add this entry under `hooks` in Claude Code settings:

```json
{"PreToolUse":[{"matcher":"mcp__akb__.*","hooks":[
  {"type":"command","command":"python3 /path/to/claude_mcp_turn.py hook"}
]}]}
```

In that server's MCP configuration, prefix its existing command and arguments with the adapter. For example, if the command is `npx -y akb-mcp`, use:

```json
{"command":"python3","args":["/path/to/claude_mcp_turn.py","--","npx","-y","akb-mcp"]}
```

Keep the server's existing environment and credentials. Both processes use the same local user cache (`XDG_CACHE_HOME` or `~/.cache`). The hook records only an exact tool-use key and two IDs. The adapter looks up that key before forwarding the request, so overlapping calls and sessions can be distinguished. Mappings expire after ten minutes and the next hook removes expired rows. A missing mapping leaves the call usable. Hook output does not change tool permissions. See the [Claude Code hook fields](https://code.claude.com/docs/en/hooks#common-input-fields).

Hover, focus or tap a chart interval to see its bounds, count and coverage. The arrow keys move between intervals and Escape dismisses the tooltip.

**Empty search results** divides successful `akb_search` responses returning zero items by successful responses with a measured result count. It shows a dash when the denominator is zero. **Browse latency** reports the nearest-rank p95 of successful measured `akb_browse` calls. Group by page size, depth, page position, or their combination. Cursor paging uses its effective page size (100 when omitted); legacy limit/offset calls form a separate size group. A cursor or a positive legacy offset marks a subsequent page. Both panels respect the report filters. Observations from before these measurements were enabled are excluded. No search text, result content or cursor values are stored.

## Declaring a tool metric

In the server tool catalog, use `measured_tool` in place of `Tool` and supply each metric alongside the existing name and input schema. The dispatcher reads the arguments and business result inside the individual request. A declaration is private to the server. Both transports use the same tool definition.

```python
measured_tool(
    ZeroRate("empty_results", "Empty lookup results", "returned"),
    Latency("batch_latency", "Batch latency", (
        Dimension("batch_size", "Batch size", lambda args, _: args.get("batch_size", 10)),
    )),
    name="example_lookup",
    inputSchema={"type": "object", "properties": {"batch_size": {"type": "integer"}}},
)
```

`ZeroRate` reads a nonnegative integer result field and stores whether it was zero. Only successful responses with that field contribute to the denominator. `Latency` groups the server duration by the declared dimensions. Each reader projects a bounded integer, boolean or explicit null. For example, a reader can turn cursor presence into a boolean. A missing required field, invalid value or failed reader skips that metric. Ordinary tool counts continue independently.

Use short alphanumeric identifiers with underscores or hyphens for metric and dimension keys. Keep each metric key stable within its tool and assign a fresh key when changing its meaning or dimensions. The collector writes each measured definition and its observations in the same transaction. The report returns panels for active declarations, including those with zero samples. Removing a declaration or the whole tool keeps its panel visible as a historical metric in windows that still contain its measurements. Observations already recorded with the older search and browse columns participate in the same aggregates.

The admin response lists panels in `metrics`. Each describes its tool, key, metric kind, dimensions, display labels and groups. Send a repeated `metric_view=tool/key/dimension/page` query parameter to regroup or paginate individual panels. Use `*` for all dimensions and zero for the first page. p95 is computed from the underlying call durations for each selected grouping.
