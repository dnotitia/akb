# Two-Keycloak broker-chain fixture

This disposable fixture proves the `keycloak-oidc` contribution against two
real Keycloak 26.7 processes. It configures the upstream provider disabled,
reads it back, proves masked-secret preservation on a disabled reconfigure,
enables it, verifies `kc_idp_hint` redirects to the upstream realm, disables
it, and prints a secret-free JSON receipt.

Requirements: Docker Compose, OpenSSL, curl, and `uv`.

```bash
deploy/keycloak-dev/broker-chain/run.sh
```

The runner generates a one-day self-signed certificate in a private temporary
directory, uses a unique Compose project, and always tears down containers,
networks, volumes, and certificates. All committed credentials are explicitly
fixture-only. The runner uses `--insecure`/`verify_ssl=False` only for its
ephemeral certificate; production SSO must verify TLS.

The fixed host ports are `19443` for the broker and `19444` for the upstream
realm. Do not run this fixture against a shared Keycloak or reuse its realm
files as production credentials.
