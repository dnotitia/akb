// Seed the harness Keycloak with the state each page needs. Idempotent per
// fresh container; run.sh calls it once. Every credential here is a
// fixture-only value for a container that exists for one run.
export const BASE = process.env.AKB_THEME_KEYCLOAK ?? "http://localhost:18480";
export const APP_REDIRECT = "http://localhost:18999/callback";
export const PASSWORDS = {
  ada: "fixture-only-ada-password", // pragma: allowlist secret
  recovery: "fixture-only-recovery-password", // pragma: allowlist secret
  upstream: "fixture-only-upstream-password", // pragma: allowlist secret
  local: "fixture-only-local-password", // pragma: allowlist secret
};
const BROKER_SECRET = "fixture-only-broker-secret"; // pragma: allowlist secret

async function call(method, path, token, body) {
  const response = await fetch(`${BASE}${path}`, {
    method,
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok && response.status !== 409) {
    throw new Error(`${method} ${path}: ${response.status} ${await response.text()}`);
  }
  return response;
}

async function adminToken() {
  const response = await fetch(`${BASE}/realms/master/protocol/openid-connect/token`, {
    method: "POST",
    body: new URLSearchParams({
      grant_type: "password",
      client_id: "admin-cli",
      username: "fixture-admin",
      password: "fixture-only-admin-password", // pragma: allowlist secret
    }),
  });
  if (!response.ok) throw new Error(`admin token: ${response.status}`);
  return (await response.json()).access_token;
}

function user(username, { email, first = "Fixture", last = "Person", password, temporary = false, actions = [] } = {}) {
  return {
    username,
    email,
    emailVerified: Boolean(email),
    enabled: true,
    firstName: first,
    lastName: last,
    requiredActions: actions,
    credentials: password ? [{ type: "password", value: password, temporary }] : [],
  };
}

export async function seed() {
  const token = await adminToken();

  await call("POST", "/admin/realms", token, {
    realm: "workforce",
    enabled: true,
    loginWithEmailAllowed: true,
    clients: [
      {
        clientId: "akb-broker",
        enabled: true,
        publicClient: false,
        secret: BROKER_SECRET,
        standardFlowEnabled: true,
        redirectUris: [`${BASE}/realms/akb/broker/entra/endpoint`],
      },
    ],
    users: [
      user("bob", { email: "bob@example.com", password: PASSWORDS.upstream }),
      user("carol", { email: "carol@example.com", last: "", password: PASSWORDS.upstream }),
    ],
  });

  // Upstream accounts may lack a last name (carol does). Without this the
  // upstream realm would ask her for it before AKB ever sees her.
  const profile = await (await call("GET", "/admin/realms/workforce/users/profile", token)).json();
  for (const attribute of profile.attributes) {
    if (attribute.name === "lastName") delete attribute.required;
  }
  await call("PUT", "/admin/realms/workforce/users/profile", token, profile);

  await call("POST", "/admin/realms", token, {
    realm: "akb",
    enabled: true,
    displayName: "AKB",
    loginTheme: "akb",
    internationalizationEnabled: true,
    supportedLocales: ["en", "ko"],
    defaultLocale: "en",
    registrationAllowed: false,
    resetPasswordAllowed: false,
    loginWithEmailAllowed: false,
    duplicateEmailsAllowed: false,
  });

  for (const name of ["akb:vault:read", "akb:vault:write"]) {
    await call("POST", "/admin/realms/akb/client-scopes", token, {
      name,
      protocol: "openid-connect",
      attributes: {
        "consent.screen.text": name.endsWith("read")
          ? "Read your AKB vaults (documents, tables, files, search)"
          : "Create, edit, and delete AKB content",
        "display.on.consent.screen": "true",
        "include.in.token.scope": "true",
      },
    });
  }

  const client = (clientId, { attributes = {}, ...extra } = {}) =>
    call("POST", "/admin/realms/akb/clients", token, {
      clientId,
      enabled: true,
      publicClient: true,
      standardFlowEnabled: true,
      directAccessGrantsEnabled: false,
      redirectUris: [APP_REDIRECT],
      webOrigins: ["+"],
      ...extra,
      attributes: { "pkce.code.challenge.method": "S256", "post.logout.redirect.uris": APP_REDIRECT, ...attributes },
    });
  await client("akb-web", { name: "AKB", baseUrl: "http://localhost:18999/" });
  await client("akb-web-admin", { name: "AKB product administration", attributes: { "akb.login.native-only": "true" } });
  await client("mcp-agent", {
    name: "Codex",
    consentRequired: true,
    defaultClientScopes: ["basic", "profile", "email"],
    optionalClientScopes: ["offline_access", "akb:vault:read", "akb:vault:write"],
  });
  await client("missing-theme", { attributes: { login_theme: "akb-missing" } });

  await call("POST", "/admin/realms/akb/identity-provider/instances", token, {
    alias: "entra",
    displayName: "teams",
    providerId: "oidc",
    enabled: true,
    config: {
      issuer: `${BASE}/realms/workforce`,
      authorizationUrl: `${BASE}/realms/workforce/protocol/openid-connect/auth`,
      tokenUrl: `${BASE}/realms/workforce/protocol/openid-connect/token`,
      userInfoUrl: `${BASE}/realms/workforce/protocol/openid-connect/userinfo`,
      jwksUrl: `${BASE}/realms/workforce/protocol/openid-connect/certs`,
      useJwksUrl: "true",
      validateSignature: "true",
      clientId: "akb-broker",
      clientSecret: BROKER_SECRET,
      clientAuthMethod: "client_secret_post",
      defaultScope: "openid profile email",
      syncMode: "FORCE",
    },
  });

  // Required actions that are off by default.
  for (const alias of ["TERMS_AND_CONDITIONS"]) {
    const response = await call("GET", `/admin/realms/akb/authentication/required-actions/${alias}`, token);
    const action = await response.json();
    await call("PUT", `/admin/realms/akb/authentication/required-actions/${alias}`, token, { ...action, enabled: true });
  }

  const users = [
    user("ada", { email: "ada@example.com", password: PASSWORDS.ada }),
    user("akb-recovery", { email: "akb-recovery@example.com", password: PASSWORDS.recovery, temporary: true, actions: ["UPDATE_PASSWORD"] }),
    // Same address as the upstream's bob, never linked: first broker login
    // stops on "account already exists".
    user("bob", { email: "bob@example.com", password: PASSWORDS.local }),
    user("casey", { email: "casey@example.com", password: PASSWORDS.local, actions: ["CONFIGURE_TOTP"] }),
    user("dana", { email: "dana@example.com", password: PASSWORDS.local, actions: ["UPDATE_PROFILE"] }),
    user("erin", { email: "erin@example.com", password: PASSWORDS.local, actions: ["TERMS_AND_CONDITIONS"] }),
  ];
  for (const entry of users) {
    await call("POST", "/admin/realms/akb/users", token, entry);
  }
}

const isMain = process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href;
if (isMain) {
  seed().then(
    () => console.log("seeded"),
    (error) => {
      console.error(error.message);
      process.exit(1);
    },
  );
}
