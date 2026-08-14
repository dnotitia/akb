# Two-Keycloak broker-chain fixture

This disposable fixture proves the `keycloak-oidc` contribution against two
real Keycloak 26.7 processes and a temporary PostgreSQL 16 database. It
configures the upstream provider disabled, proves masked-secret preservation,
and uses a one-time fixture operator to create one native broker user and exact
federated link. The permanent management account then reads that link back
with its exact six-role set and without `manage-users`.

The fixture completes Authorization Code + PKCE login through both realms,
verifies the broker access token against AKB's production verifier, and proves
that it resolves to the existing AKB user while the user's PAT, owned Vault,
and writer grant remain valid. A separate upstream token is deliberately given
the AKB API audience and still must be rejected. Finally, the provider is
disabled, the AKB identity binding is rolled back, the operator removes the
Keycloak prelink/user, and a secret-free JSON receipt is printed.

Requirements: Docker Compose, OpenSSL, curl, and `uv`.

```bash
deploy/keycloak-dev/broker-chain/run.sh
```

The runner generates a one-day self-signed certificate and minimal AKB config
in private temporary directories, uses a unique Compose project, and verifies
that teardown leaves no labeled container, network, or volume. All committed
credentials are explicitly fixture-only. The runner uses
`--insecure`/`verify_ssl=False` only for its ephemeral certificate; production
SSO must verify TLS.

The fixed host ports are `19443` for the broker, `19444` for the upstream realm,
and `19445` for PostgreSQL. All three bind to `127.0.0.1` only. Do not run this
fixture against a shared Keycloak or reuse its realm files as production
credentials.
