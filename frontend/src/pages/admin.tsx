import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Logo } from "@/components/logo";
import { ThemeToggle } from "@/components/theme-toggle";
import {
  adminLocalLogin,
  adminLogout,
  getAdminAuthConfig,
  getAdminSession,
  setToken,
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

      <main className="relative z-10 mx-auto flex min-h-[calc(100dvh-4rem)] w-full max-w-lg items-center px-5 py-12 sm:px-8">
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
            <p className="text-sm text-foreground-muted" role="status">Verifying admin policy…</p>
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
