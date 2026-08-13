# Adding a built-in SSO provider

This is the contribution contract for provider integrations shipped by AKB.
It keeps provider setup consistent without turning the admin page into an
arbitrary Keycloak representation editor.

## Source layout

```text
backend/app/sso/
├── models.py
├── registry.py
└── providers/
    └── <provider_type>.py

docs/sso/providers/
└── <provider_type>.md
```

Add the provider module to the explicit map in `backend/app/sso/registry.py`.
Do not use import scanning, entry points, uploaded code, or configuration that
selects a Python module at runtime.

## Provider module contract

A provider definition must expose a stable lowercase `PROVIDER_TYPE`, the
Keycloak provider ID it renders, and these bounded operations:

1. Validate a provider-neutral configuration. Reject unknown provider types,
   unsafe aliases, invalid URLs, and the broker's own issuer.
2. Render a complete disabled Keycloak representation from an allowlist.
   Never pass through an imported discovery response wholesale.
3. Mark the representation with AKB's provider type and schema version.
4. Toggle only `enabled` and `hideOnLogin` for activation changes.
5. Read the representation back into `configured_disabled`, `enabled`, or
   `configuration_error` using an exact security profile.
6. Produce separate admin, public, and audit views. None may contain a client
   secret or Keycloak's masked-secret sentinel.

Provider errors must use stable value-less codes. Do not include request
values, upstream bodies, tokens, or secrets in exception messages.

## Security defaults

The rendered profile must be secure by construction. For OIDC providers this
normally includes authorization code flow, signature validation, JWKS, an
exact issuer, PKCE S256 where supported, and a bounded scope set. Trusting
email, storing upstream tokens, broad token roles, implicit flow, password
grant, or account linking by email require an explicit design review and must
not be enabled as convenience defaults.

Configuration always lands disabled. Creation requires a client secret;
reconfiguration may use Keycloak's masked-secret preservation sentinel only
after exact read-back proved that the existing managed provider has a secret.

## Required tests

Contributions must cover:

- accepted and rejected configuration boundaries;
- exact rendered representation and ignored discovery fields;
- secret creation, preservation, rotation, and redaction;
- unmanaged alias collision;
- disabled-before-reconfigure enforcement;
- enable, disable, idempotency, and read-back drift;
- public catalog inclusion only for an exact enabled profile;
- strict frontend parsing and a named provider button;
- a live two-authority fixture when the provider depends on another identity
  server.

The live fixture must use disposable credentials and namespaces. It must not
modify a shared Keycloak realm or depend on an internal-only host in public
repository content.
