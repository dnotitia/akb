import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { Logo } from "@/components/logo";
import { ThemeToggle } from "@/components/theme-toggle";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  adminLocalLogin,
  adminLogout,
  configureAdminSsoProvider,
  getAdminAuthConfig,
  getAdminSession,
  getAdminSsoCatalog,
  setAdminSsoProviderEnabled,
  setToken,
  type AdminSsoProvider,
} from "@/lib/api";


export default function AdminPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const configQuery = useQuery({
    queryKey: ["admin-auth-config", 1],
    queryFn: getAdminAuthConfig,
    staleTime: 30_000,
  });
  const config = configQuery.data;
  const sessionQuery = useQuery({
    queryKey: ["admin-session", 1],
    queryFn: getAdminSession,
    enabled: config?.available === true,
    staleTime: 10_000,
  });
  const session =
    sessionQuery.data?.auth_mode === config?.auth_mode
      ? sessionQuery.data
      : undefined;

  async function submitLocal(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      const result = await adminLocalLogin(username, password);
      if (!result.token || typeof result.token !== "string") {
        throw new Error(result.error || "The server returned no admin session");
      }
      setToken(result.token);
      setPassword("");
      await sessionQuery.refetch();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Product-admin sign-in failed");
    } finally {
      setSubmitting(false);
    }
  }

  function startKeycloak() {
    if (config?.keycloak.login_url) {
      window.location.assign(config.keycloak.login_url);
    }
  }

  async function signOut() {
    setError("");
    setSubmitting(true);
    try {
      const result = await adminLogout();
      if (session?.auth_mode === "local") setToken(null);
      if (session?.auth_mode === "sso") {
        window.location.assign(result.logout_url);
        return;
      }
      await sessionQuery.refetch();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Sign-out failed");
    } finally {
      setSubmitting(false);
    }
  }

  const unavailable =
    configQuery.isError ||
    (configQuery.isSuccess && config?.available !== true);

  return (
    <div className="relative min-h-dvh overflow-hidden bg-background text-foreground">
      <div className="aurora-bg" aria-hidden />
      <header className="relative z-10 flex h-16 items-center justify-between border-b border-border bg-surface/80 px-5 backdrop-blur-md sm:px-8">
        <Logo size={30} subtitle />
        <ThemeToggle />
      </header>

      <main className="relative z-10 mx-auto flex min-h-[calc(100dvh-4rem)] w-full max-w-3xl items-center px-5 py-12 sm:px-8">
        <section className="w-full rounded-[var(--radius-xl)] border border-border bg-surface p-6 shadow-lg sm:p-8" aria-labelledby="admin-heading">
          <div className="mb-7">
            <p className="coord mb-2 text-primary">CONTROL PLANE</p>
            <h1 id="admin-heading" className="font-display text-2xl font-semibold tracking-tight">
              Product administration
            </h1>
            <p className="mt-2 text-sm leading-6 text-foreground-muted">
              This sign-in is separate from the ordinary AKB user surface.
            </p>
          </div>

          {(configQuery.isPending || (config?.available && sessionQuery.isPending)) && (
            <AdminPolicyLoading />
          )}

          {unavailable && (
            <Alert variant="destructive">
              Product-admin authentication configuration could not be verified.
            </Alert>
          )}

          {sessionQuery.isError && (
            <Alert variant="destructive">
              The current product-admin session could not be verified.
            </Alert>
          )}

          {session && (
            <div className="space-y-5">
              <div className="rounded-[var(--radius-lg)] border border-border bg-surface-2 p-4">
                <p className="font-medium">{session.user.display_name || session.user.username}</p>
                <p className="mt-1 text-sm text-foreground-muted">{session.user.email}</p>
                <p className="coord mt-3">{session.auth_mode.toUpperCase()} ADMIN SESSION</p>
              </div>
              {session.auth_mode === "sso" && <SsoProviderControl />}
              {error && <Alert variant="destructive">{error}</Alert>}
              <Button type="button" variant="outline" className="w-full" loading={submitting} onClick={signOut}>
                Sign out
              </Button>
            </div>
          )}

          {!session && sessionQuery.isSuccess && config?.available && config.auth_mode === "local" && config.local.enabled && (
            <form className="space-y-4" onSubmit={submitLocal}>
              <div>
                <label className="mb-1.5 block text-sm font-medium" htmlFor="admin-username">Username</label>
                <Input id="admin-username" name="username" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required autoFocus />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium" htmlFor="admin-password">Password</label>
                <Input id="admin-password" name="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required />
              </div>
              {error && <Alert variant="destructive">{error}</Alert>}
              <Button type="submit" size="lg" className="w-full" loading={submitting}>
                Sign in
              </Button>
            </form>
          )}

          {!session && sessionQuery.isSuccess && config?.available && config.auth_mode === "sso" && config.keycloak.enabled && (
            <div className="space-y-4">
              {error && <Alert variant="destructive">{error}</Alert>}
              <Button type="button" size="lg" className="w-full" onClick={startKeycloak}>
                Sign in with Keycloak
              </Button>
              <p className="text-xs leading-5 text-foreground-muted">
                Only an identity pre-bound to an active AKB product administrator is accepted.
              </p>
            </div>
          )}

          <div className="mt-7 border-t border-border pt-5 text-center">
            <Link className="text-sm text-link hover:text-link-hover hover:underline" to="/auth">
              Go to ordinary user sign-in
            </Link>
          </div>
        </section>
      </main>
    </div>
  );
}

const EMPTY_PROVIDER_FORM = {
  alias: "",
  displayName: "",
  issuer: "",
  clientId: "",
  clientSecret: "",
};

function providerBadge(state: AdminSsoProvider["state"]) {
  if (state === "enabled") return <Badge variant="success">Enabled</Badge>;
  if (state === "configuration_error") return <Badge variant="error">Configuration error</Badge>;
  return <Badge variant="pending">Disabled</Badge>;
}

function SsoProviderControl() {
  const [form, setForm] = useState(EMPTY_PROVIDER_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [togglingAlias, setTogglingAlias] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const catalogQuery = useQuery({
    queryKey: ["admin-sso-catalog", 1],
    queryFn: getAdminSsoCatalog,
    staleTime: 10_000,
  });
  const catalog = catalogQuery.data;

  function setField(field: keyof typeof form, value: string) {
    setForm((current) => ({ ...current, [field]: value }));
    setError("");
    setNotice("");
  }

  function editProvider(provider: AdminSsoProvider) {
    setForm({
      alias: provider.alias,
      displayName: provider.display_name,
      issuer: provider.issuer || "",
      clientId: provider.client_id || "",
      clientSecret: "",
    });
    setError("");
    setNotice("Leave the client secret blank to preserve the configured value.");
  }

  async function saveProvider(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setNotice("");
    setSubmitting(true);
    const issuer = form.issuer.trim().replace(/\/+$/, "");
    try {
      await configureAdminSsoProvider(form.alias.trim(), {
        provider_type: "keycloak-oidc",
        display_name: form.displayName.trim(),
        issuer,
        discovery_url: `${issuer}/.well-known/openid-configuration`,
        client_id: form.clientId.trim(),
        ...(form.clientSecret ? { client_secret: form.clientSecret } : {}),
      });
      setForm((current) => ({ ...current, clientSecret: "" }));
      setNotice("Provider configuration was saved disabled. Enable it after checking the redirect URI.");
      await catalogQuery.refetch();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Provider configuration failed");
    } finally {
      setSubmitting(false);
    }
  }

  async function toggleProvider(provider: AdminSsoProvider) {
    const enabled = !provider.enabled;
    setError("");
    setNotice("");
    setTogglingAlias(provider.alias);
    try {
      await setAdminSsoProviderEnabled(provider.alias, enabled);
      setNotice(`${provider.display_name} was ${enabled ? "enabled" : "disabled"}.`);
      await catalogQuery.refetch();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Provider state change failed");
    } finally {
      setTogglingAlias(null);
    }
  }

  if (catalogQuery.isPending) {
    return <SsoProvidersLoading />;
  }
  if (catalogQuery.isError || !catalog) {
    return <Alert variant="destructive">SSO provider configuration could not be loaded.</Alert>;
  }
  if (catalog.control_mode === "delegated") {
    return (
      <Alert variant="info" title="SSO providers are managed by the deployment operator">
        This installation does not expose direct Keycloak provider control.
      </Alert>
    );
  }

  return (
    <div className="space-y-4" aria-labelledby="sso-providers-heading">
      <div>
        <h2 id="sso-providers-heading" className="font-display text-xl font-semibold tracking-tight">
          SSO providers
        </h2>
        <p className="mt-1 text-sm leading-6 text-foreground-muted">
          Enabled providers become the only ordinary-user sign-in options. Changes do not require a redeploy.
        </p>
      </div>

      {error && <Alert variant="destructive">{error}</Alert>}
      {notice && <Alert variant="success">{notice}</Alert>}

      {catalog.providers.length === 0 ? (
        <Alert variant="info">No upstream SSO provider is configured yet.</Alert>
      ) : (
        <Panel flush>
          <PanelHeader label="Configured providers" count={catalog.providers.length} />
          <div className="divide-y divide-border">
            {catalog.providers.map((provider) => (
              <div key={provider.alias} className="p-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="font-medium">{provider.display_name}</p>
                      {providerBadge(provider.state)}
                    </div>
                    <p className="coord mt-1">{provider.alias} · {provider.provider_type}</p>
                    <p className="mt-2 break-all text-xs leading-5 text-foreground-muted">
                      Redirect URI: {provider.redirect_uri}
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button type="button" size="sm" variant="outline" onClick={() => editProvider(provider)}>
                      Edit
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant={provider.enabled ? "outline" : "default"}
                      disabled={provider.state === "configuration_error" && !provider.enabled}
                      loading={togglingAlias === provider.alias}
                      onClick={() => toggleProvider(provider)}
                    >
                      {provider.enabled ? "Disable" : "Enable"}
                    </Button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </Panel>
      )}

      <Panel inset={false} flush>
        <PanelHeader label="Keycloak OIDC provider" />
        <form className="grid gap-4 p-4 sm:grid-cols-2" onSubmit={saveProvider}>
          <AdminField label="Alias" id="sso-alias" value={form.alias} onChange={(value) => setField("alias", value)} pattern="[a-z0-9][a-z0-9._-]{0,62}" autoComplete="off" required />
          <AdminField label="Button label" id="sso-display-name" value={form.displayName} onChange={(value) => setField("displayName", value)} maxLength={80} autoComplete="organization" required />
          <div className="sm:col-span-2">
            <AdminField label="Upstream issuer" id="sso-issuer" type="url" value={form.issuer} onChange={(value) => setField("issuer", value)} placeholder="https://id.example.com/realms/workforce" autoComplete="url" required />
          </div>
          <AdminField label="Client ID" id="sso-client-id" value={form.clientId} onChange={(value) => setField("clientId", value)} autoComplete="off" required />
          <AdminField label="Client secret" id="sso-client-secret" type="password" value={form.clientSecret} onChange={(value) => setField("clientSecret", value)} placeholder="Required for a new provider" autoComplete="new-password" />
          <div className="sm:col-span-2">
            <Button type="submit" loading={submitting}>Save disabled configuration</Button>
            <p className="mt-2 text-xs leading-5 text-foreground-muted">
              Discovery uses the issuer&apos;s standard .well-known endpoint. Secrets are write-only.
            </p>
          </div>
        </form>
      </Panel>
    </div>
  );
}

function AdminPolicyLoading() {
  return (
    <LoadingState label="Verifying admin policy">
      <div className="space-y-4">
        <div className="rounded-[var(--radius-lg)] border border-border bg-surface-2 p-4">
          <Skeleton className="h-4 w-36 rounded-[var(--radius-sm)]" />
          <Skeleton className="mt-3 h-3 w-52 rounded-[var(--radius-sm)]" />
          <Skeleton className="mt-4 h-3 w-28 rounded-[var(--radius-sm)]" />
        </div>
        <Skeleton className="h-10 w-full rounded-[var(--radius-md)]" />
      </div>
    </LoadingState>
  );
}

function SsoProvidersLoading() {
  return (
    <LoadingState label="Loading SSO providers" className="space-y-4">
      <div>
        <Skeleton className="h-6 w-40 rounded-[var(--radius-md)]" />
        <Skeleton className="mt-2 h-4 w-3/4 rounded-[var(--radius-sm)]" />
      </div>
      <div className="overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface">
        <div className="border-b border-border p-4">
          <Skeleton className="h-4 w-36 rounded-[var(--radius-sm)]" />
        </div>
        {[0, 1].map((item) => (
          <div key={item} className="flex items-center gap-4 border-b border-border p-4 last:border-b-0">
            <div className="min-w-0 flex-1 space-y-2">
              <Skeleton className="h-4 w-40 rounded-[var(--radius-sm)]" />
              <Skeleton className="h-3 w-64 max-w-full rounded-[var(--radius-sm)]" />
            </div>
            <Skeleton className="h-9 w-20 rounded-[var(--radius-md)]" />
          </div>
        ))}
      </div>
    </LoadingState>
  );
}

function AdminField({
  label,
  id,
  value,
  onChange,
  ...props
}: {
  label: string;
  id: string;
  value: string;
  onChange: (value: string) => void;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "id" | "value" | "onChange">) {
  return (
    <div>
      <label className="mb-1.5 block text-sm font-medium" htmlFor={id}>{label}</label>
      <Input id={id} value={value} onChange={(event) => onChange(event.target.value)} {...props} />
    </div>
  );
}
