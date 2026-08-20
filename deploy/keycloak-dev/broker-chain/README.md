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

It also asks a separate question about a differently shaped account. The
prelink above is an operator's: marked verified, and linked to the upstream
subject by hand. An account created by member invitation is neither — it is
enabled, carries no credential and no required action, its address is
unverified, and nothing is linked to it — so whether the invited person's first
login through the broker lands on THAT account is a claim about Keycloak's
first-broker-login rather than about this codebase. The phase creates the
seeded shape, checks every property of it rather than assuming it, supplies the
invited person's upstream credential and nothing else, and records which
account the login landed on, what became of the address's verified flag, and
every page demanded along the way. It stops at the first page beyond the
credential rather than answering it, because a step the invited person cannot
complete is a failure and not a slow success.

It also measures where the authority to mint the product administrator's
credential actually lives, against the same realm. Three facts in one phase: the
permanent management account is refused `403` when it tries to reset that
password; an authority created for the purpose succeeds, and the credential it
installs is one use, because the realm demands a replacement at first login;
and once that authority is retired, neither the token it was holding nor a newly
requested one is accepted. The replaced credential is then refused at a real
login page, and the new one is accepted straight into the forced change — proved
at the door an administrator uses, not read back out of the store that was just
written.

The transient authority is created the way a deployment creates one, with
Keycloak's own `bootstrap-admin service` command in a separate process against
the running broker's database. Nothing standing in the realm can create it,
which is the point of the phase, so the fixture cannot mint it through the Admin
REST API either. That is why the broker keeps its state in the fixture's
PostgreSQL rather than an embedded H2 file: a second process cannot open H2, and
faking the creation would leave the real path unexercised while the fixture
reported it verified.

Requirements: Docker Compose, OpenSSL, curl, and `uv`. Two host conditions are
easy to miss because the runner can only report them as "Keycloak never became
ready": `broker.localhost` and `upstream.localhost` must resolve to `127.0.0.1`
(some resolvers do this for `*.localhost` and some do not — add them to
`/etc/hosts` if `getent hosts broker.localhost` prints nothing), and a host
HTTP proxy must not capture them. The runner names both hosts in `NO_PROXY` for
the exercise itself; a proxy that intercepts them anyway surfaces as an
"unreachable" Keycloak that `curl --insecure` can reach perfectly well.

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
and `19445` for PostgreSQL. The one PostgreSQL process serves two databases —
AKB's and the broker Keycloak's — on the same tmpfs volume, so teardown destroys
both. All three bind to `127.0.0.1` only. Do not run this
fixture against a shared Keycloak or reuse its realm files as production
credentials.
