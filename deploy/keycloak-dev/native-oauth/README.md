# Real native OAuth fixture

This specialist fixture owns one disposable Keycloak instance. It uses the
broker-chain fixture's isolated Compose lifecycle with a unique project,
private runtime directory, dynamic loopback port, and verified teardown.
The common E2E runtime remains the owner of AKB, PostgreSQL, MinIO, PAT fixtures,
and stdio consumers.

The fixture calls `scripts/keycloak/setup-akb-mcp-oauth.py` from the selected
checkout twice. It independently rereads the real realm and client to verify
public Authorization Code with S256, the exact loopback callback path, human
claim scopes, the exact MCP audience mapper, and zero-reuse refresh rotation.
This command verifies provisioning. It does not establish browser, signed-token,
proxy OAuth, or native-host acceptance.

Install the backend's locked dependencies and cache the pinned Keycloak image
before running the fixture. It uses `--pull never` and never changes Docker
authentication settings. Inject `AKB_E2E_USERNAME` and `AKB_E2E_PASSWORD` into the
environment. The fixture creates a separate realm human named with an `oauth-`
prefix and passes credentials only through environment and request bodies on
its disposable HTTP loopback endpoint. No credential enters argv or
the report. The loopback HTTP endpoint is a development fixture only.

```bash
uv run --locked --project backend python \
  deploy/keycloak-dev/native-oauth/fixture.py \
  --checkout "$PWD" \
  --resource http://127.0.0.1:8000/mcp
```

`--checkout` selects the candidate's production provisioning script. An external
immutable source archive can be extracted outside the working checkout and
selected here. `--runtime-root` selects an external private directory. The
secret-free receipt and private command logs remain there after containers,
networks, volumes, and the listener have been removed.

`--provisioner` selects an explicit immutable production script artifact when
testing an operator-script change before the next combined source archive.
The receipt records its path and SHA256 separately from the candidate checkout.
Such a run proves that script against the real AS, not a final combined product.
