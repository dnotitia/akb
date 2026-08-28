# Repository MCP Inspector tooling

This private package owns the exact `@modelcontextprotocol/inspector@2.4.0`
development dependency used by the repository's consumer smoke. It is not a
runtime dependency of `akb-mcp` and is not published.

Install and run the shared repository entrypoint:

```bash
npm ci
npm run inspect -- --intent smoke --target both --descriptor /path/to/descriptor.json
```

The descriptor comes from the existing schema-v2 E2E runtime. Smoke uses the
modern protocol era and reports machine-readable evidence for HTTP and stdio;
interactive intent opens Inspector Web on loopback with its normal
authentication.
