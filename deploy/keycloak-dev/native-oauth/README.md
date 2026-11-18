# Packed OAuth CLI acceptance with Keycloak

This fixture runs the installed npm tarball, AKB, and a disposable Keycloak
realm. It drives a real Authorization Code login in an isolated Chromium
profile. Keycloak provisioning uses the repository's operator script and
anonymous OpenID Connect client registration. The fixture generates its own
test accounts and grant store. The common E2E runtime starts PostgreSQL, MinIO,
and AKB in local browser mode with Bare Git revisions.

Use Node 24 for the proxy and install the backend's locked dependencies. Install
the frontend's locked dependencies with its pinned Node version, and install
Playwright Chromium from the frontend. Cache the pinned Keycloak image from
`compose.yaml`. The fixture starts Keycloak with `--pull never`. Install the
`codex` and `claude` executables to include both native hosts.

Run from the checkout:

```bash
runtime_root="$(mktemp -d /tmp/akb-native-oauth.XXXXXX)"
uv run --locked --project backend python deploy/keycloak-dev/native-oauth/run.py \
  --checkout "$PWD" \
  --runtime-root "$runtime_root" \
  --compose-file "$PWD/scripts/ci/dependency-compose.yaml" \
  --files --hosts --prefix /nested/workspace
```

Omit `--prefix` for the origin-root resource. A longer consent separation check
can use `--consent-pause-seconds 120`. The CLI's browser login has a bounded
deadline which includes this pause.

The runner checks cold stdio startup, a competing login, two real PKCE logins
through one registration, and a fresh stdio process listing and calling a normal
tool. It verifies the signed access token against real JWKS and observes
rotating refresh across two stdio processes. It checks shared logout and
checks that a tool call rejected after logout stays unexecuted after a separate
login. The npm consumer installs only production dependencies from the tarball.

`--files` runs the existing local file suites with the packed executable and
checks file replacement, concurrency pins, byte integrity, image upload and
discard. The loopback observer compares bearer fingerprints for MCP and the
eight file and image control operations. Byte capability requests must carry
no general bearer. A committed image is reread through the existing runtime's
PAT for an independent bytes and MIME check.

`--hosts` uses isolated temporary Codex and Claude Code configurations. Codex
uses its app-server to list and call the real MCP tool. Claude Code runs in
print mode against a deterministic local model endpoint that requests the
same tool and receives the returned Vault list. Each host uses a fresh home,
provider configuration, and session directory, plus the fixture's OAuth
grant store.

The runner exits nonzero on a failed check and verifies teardown of the
processes, containers, networks, volumes, and ports. Read
`oauth-report.json`, `browser-*.json`, `issuer-observations.json`, and
`resource-observations.json` in the private runtime directory for the evidence
and its scope. The observer records no raw bearer, authorization code, callback
query, verifier, or capability URL. Delete the private runtime directory when
finished with its reports and fixture state.
