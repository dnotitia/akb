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
The native defaults are `basic`, `profile`, and `email`. The `basic` scope
supplies Keycloak's human subject mapper.
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

## Run the packed browser and session check

Install the frontend's locked dependencies on the Node version required by
`frontend/package.json`. The browser driver uses that existing Playwright
dependency and its Chromium installation. Keep the same fixture credential
environment described above.

```bash
RUNTIME_ROOT="$(mktemp -d /tmp/akb-native-browser.XXXXXX)"
uv run --locked --project backend python \
  deploy/keycloak-dev/native-oauth/run.py \
  --checkout "$PWD" --runtime-root "$RUNTIME_ROOT" \
  --compose-file "$PWD/scripts/ci/dependency-compose.yaml"
```

For a source archive without `.git`, set `AKB_E2E_SOURCE_REVISION` to its verified
commit before running this command. `--compose-file` has the existing runtime's
dependency contract. A private override can select an immutable cached
PostgreSQL image without changing shared image tags or Docker authentication.

The check retains the common runtime's backend, dependency, credential, and
process ownership. After its normal PAT bootstrap, it replaces the stdio child
with the same installed tarball in URL-only OAuth mode. A private PATH opener
captures the real authorization URL. Chromium fills the real Keycloak form and
consent page, and the actual proxy owns the callback and exchange.

The receipt separates pending catalog and protocol responses, the new successful
business read, signed required claims, natural refresh rotation, restart reuse,
and old-refresh rejection. A pending mutation must remain absent after login.
`SIGINT` or `SIGTERM` stops startup and verifies cleanup of both owners. Private
reports and logs remain in the selected runtime root. A failure exits nonzero.

## Add file and prefix checks

Add `--files` to run the existing stdio file and file-content suites against
the installed archive using the shared OAuth grant. Their default standalone
mode remains PAT; `AKB_E2E_AUTH_MODE=oauth` selects the shared private grant and
`AKB_MCP_BIN` selects the installed executable. Each suite owns its temporary
files and FIFOs.

Add `--prefix /nested/workspace` to publish the real installation below a path
prefix. A transparent loopback relay forwards the real backend responses and
records request methods, sanitized paths, bearer fingerprints, and byte hashes.
It preserves origin-root protected-resource metadata paths. It records no bearer,
capability URL, query, or request body. Backend access logging is disabled in this
fixture so opaque byte capabilities do not enter request logs.

The file check observes all eight successful file/image control routes using
published OAuth generations after real refresh rotation. Byte upload/download
capabilities must carry no Authorization header, and downloaded bytes must
match uploaded bytes. The existing suites retain their skill, content hash,
optimistic concurrency, and cleanup assertions. A real image upload is also
discarded; another is referenced by a real document and independently read
using the runtime's existing PAT to check its bytes, MIME type, and dimensions.
Peer suite processes must reuse the grant without opening another browser.

These commands do not report negative-authority, concurrent-host, or native CLI
acceptance. The common runtime selects `bare_git`; they are not Native
revision-storage proof.
