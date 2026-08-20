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

Two further phases are about the people nobody linked by hand, which is
everyone a workspace admits after it owns its identity provider.

The first asserts where an account seeded ahead of arrival stops its owner.
Member invitation used to create the realm account before the person arrived,
deliberately with no credential — a member holding a realm-native password
keeps it after their organisation revokes the upstream account meant to govern
them — and left the address unverified on the stated ground that the first
accepted login would set the flag from the upstream's signed proof. That was a
claim about Keycloak, so the phase seeds exactly that shape, checks every
property of it rather than assuming it, supplies the invited person's upstream
credential and nothing else, and asserts the page they are stopped on:
confirm-link, on the broker realm, with no token issued and the verified flag
still false. The seeding ceremony is still in the codebase behind a switch,
correct only where the platform is itself the upstream, so the next person to
read that switch finds the measurement rather than the intention.

The second drives the whole admission chain. Nothing is seeded. The invited
person signs in through the upstream they already have; the broker mints their
subject; `invite_only` refuses them and the arrival is recorded with that exact
pair; an administrator approves that row; they sign in again and they are in —
and the subject is read out of a token this runtime's own verifier accepted,
not out of an admin read, because the pair an approval binds must be the pair a
token actually carries. It also proves the two properties the pre-boundary
workspace migration depends on, because that migration is the same three steps:
approving with `existing_user_id` keeps the AKB account a person already has,
with its token still authorizing and its old binding still beside the new one,
and someone who signs in twice before anyone approves them produces one record
rather than two.

Together the two are each other's control: same fixture, same run, same
credential, one page and a token when nothing was seeded and two pages and no
token when something was.

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
