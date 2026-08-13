import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, Database, Boxes, GitBranch } from "lucide-react";
import {
  authLogin,
  authRegister,
  getAuthConfig,
  getToken,
  setToken,
  type AuthConfig,
} from "@/lib/api";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { ThemeToggle } from "@/components/theme-toggle";
import { Logo } from "@/components/logo";
import { cn } from "@/lib/utils";

declare const PasswordCredential: {
  new (data: { id: string; password: string }): Credential;
};

type Mode = "login" | "register";

// Post-auth landing path stashed by the layout/api guard as ?next=. Same-site
// paths only — mirror the backend redirect guard (block scheme-relative // and
// backslash tricks) so an attacker can't bounce the user off-origin.
function safeNext(raw: string | null): string {
  if (!raw) return "/";
  return raw.startsWith("/") && !raw.startsWith("//") && !raw.includes("\\") ? raw : "/";
}

export default function AuthPage() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<Mode>("login");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  // Unknown until the versioned public policy is validated. No UI capability
  // is inferred while loading or when the fetch/schema fails.
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const next = safeNext(new URLSearchParams(window.location.search).get("next"));
  const localAuthEnabled =
    authConfig?.available === true &&
    authConfig.auth_mode === "local" &&
    authConfig.local_auth.enabled;
  const ssoConfigEnabled =
    authConfig?.available === true &&
    authConfig.auth_mode === "sso" &&
    authConfig.keycloak.enabled;
  const ssoProviders = ssoConfigEnabled ? authConfig.providers : [];
  const usableSsoProviders =
    ssoConfigEnabled && authConfig.keycloak.browser_session_ready
      ? ssoProviders.filter((provider) => provider.login_url !== null)
      : [];

  useEffect(() => {
    let cancelled = false;
    void getAuthConfig().then((config) => {
      if (cancelled) return;
      setAuthConfig(config);
      if (config.available !== true) return;
      if (config.auth_mode === "sso") {
        // A pre-cutover local JWT must never drive the SSO browser path or
        // bounce the auth guard in a redirect loop.
        if (getToken()) setToken(null);
        return;
      }
      // Already signed in (back button / bookmark)? Don't re-show the local form.
      if (getToken()) navigate(next, { replace: true });
    });
    return () => {
      cancelled = true;
    };
  }, [navigate, next]);

  function startSso(loginUrl: string | null) {
    if (!loginUrl) return;
    window.location.href = `${loginUrl}?redirect=${encodeURIComponent(next)}`;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (localAuthEnabled !== true) return;
    setError("");
    // Backend change_password() enforces 8 chars but register() does not —
    // catch it here so a user can't create a password they can never change.
    if (mode === "register" && password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setLoading(true);
    try {
      if (mode === "register") {
        const reg = await authRegister(username, email, password, displayName || undefined);
        if (reg.error) {
          setError(reg.error);
          return;
        }
      }
      const r = await authLogin(username, password);
      if (r.error || !r.token) {
        // A token-less 200 would set an empty token and bounce back here. And
        // after a successful register, an auto-login failure would strand the
        // user on the register tab (a retry hits the duplicate-account guard).
        if (mode === "register") {
          setMode("login");
          setError("Account created — please sign in.");
        } else {
          setError(r.error || "Login failed — no token returned.");
        }
        return;
      }
      setToken(r.token);
      try {
        if ("PasswordCredential" in window) {
          const cred = new PasswordCredential({ id: username, password });
          navigator.credentials.store(cred).catch(() => {});
        }
      } catch {
        // Credential store is best-effort; never block navigation on it.
      }
      navigate(next);
    } catch (err: any) {
      setError(err?.message || "Something went wrong. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background text-foreground p-6">
      <div className="absolute right-4 top-4 z-10">
        <ThemeToggle />
      </div>

      <main className="relative w-full max-w-5xl grid lg:grid-cols-2 gap-10 lg:gap-16 items-center fade-up">
        {/* Page heading for small screens, where the visual hero (and its h1)
            is display:none — keeps every breakpoint with exactly one h1. */}
        <h1 className="sr-only lg:hidden">AKB — the base your agents remember</h1>
        {/* LEFT — brand hero */}
        <section className="hidden lg:flex flex-col gap-8 pr-4">
          <Logo size={42} subtitle />
          <div>
            <h1 className="font-display text-3xl font-semibold tracking-tight text-foreground">
              The base your<br />agents remember.
            </h1>
            <p className="mt-5 text-[15px] leading-relaxed text-foreground-muted max-w-md">
              A unified knowledge base for AI agents — documents, tables, and files
              under one structured, git-versioned root, served over MCP.
            </p>
          </div>
          <ul className="flex flex-col gap-3">
            {[
              [Database, "knowledge", "Hybrid semantic + keyword search"],
              [Boxes, "memory", "Documents · tables · files in one vault"],
              [GitBranch, "agent", "Git-versioned, MCP-native, multi-agent"],
            ].map(([Icon, eyebrow, text], i) => (
              <li key={i} className="flex items-center gap-3">
                <span className={cn("feature-tile", `feat-${eyebrow as string}`)} style={{ width: 34, height: 34 }}>
                  <Icon size={17} strokeWidth={1.75} aria-hidden />
                </span>
                <span className="text-sm text-foreground-muted">{text as string}</span>
              </li>
            ))}
          </ul>
        </section>

        {/* RIGHT — auth card */}
        <section className="hero-glow w-full max-w-md mx-auto">
          <div className="lg:hidden mb-8 flex justify-center">
            <Logo size={40} subtitle />
          </div>
          <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-lg p-7 sm:p-8">
            {authConfig === null && (
              <div className="py-8 text-center coord" role="status" aria-live="polite">
                Loading sign-in options…
              </div>
            )}

            {localAuthEnabled && (
              <Tabs
                value={mode}
                onValueChange={(v) => {
                  setMode(v as Mode);
                  setError("");
                }}
              >
                <TabsList className="mb-6 grid w-full grid-cols-2">
                  <TabsTrigger value="login" className="justify-center">Log in</TabsTrigger>
                  <TabsTrigger value="register" className="justify-center">Register</TabsTrigger>
                </TabsList>

                <TabsContent value="login">
                  <AuthForm
                    mode="login"
                    username={username} setUsername={setUsername}
                    password={password} setPassword={setPassword}
                    error={error} loading={loading} onSubmit={handleSubmit}
                  />
                </TabsContent>
                <TabsContent value="register">
                  <AuthForm
                    mode="register"
                    username={username} setUsername={setUsername}
                    email={email} setEmail={setEmail}
                    displayName={displayName} setDisplayName={setDisplayName}
                    password={password} setPassword={setPassword}
                    error={error} loading={loading} onSubmit={handleSubmit}
                  />
                </TabsContent>
              </Tabs>
            )}

            {authConfig !== null && !localAuthEnabled && authConfig.available !== true && (
              <Alert variant="destructive">
                Sign-in is unavailable because authentication configuration could not be verified.
              </Alert>
            )}

            {ssoConfigEnabled && ssoProviders.length === 0 && (
              <Alert variant="destructive">
                No SSO providers are enabled. Contact your administrator.
              </Alert>
            )}

            {ssoConfigEnabled && ssoProviders.length > 0 && usableSsoProviders.length === 0 && (
              <Alert variant="destructive">
                SSO browser sign-in is not available yet. Contact your administrator.
              </Alert>
            )}

            {usableSsoProviders.length > 0 && (
              <div className="space-y-3">
                {usableSsoProviders.map((provider) => (
                  <Button
                    key={provider.alias}
                    type="button"
                    variant="outline"
                    size="lg"
                    className="w-full"
                    onClick={() => startSso(provider.login_url)}
                  >
                    Sign in with {provider.display_name}
                  </Button>
                ))}
              </div>
            )}
          </div>

          <p className="mt-5 text-center coord">Dnotitia · Seahorse · v1.0</p>
        </section>
      </main>
    </div>
  );
}

interface AuthFormProps {
  mode: Mode;
  username: string;
  setUsername: (v: string) => void;
  email?: string;
  setEmail?: (v: string) => void;
  displayName?: string;
  setDisplayName?: (v: string) => void;
  password: string;
  setPassword: (v: string) => void;
  error: string;
  loading: boolean;
  onSubmit: (e: React.FormEvent) => void;
}

function AuthForm({
  mode,
  username, setUsername,
  email = "", setEmail,
  displayName = "", setDisplayName,
  password, setPassword,
  error, loading, onSubmit,
}: AuthFormProps) {
  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <Field label="Username" id="auth-username" value={username} onChange={setUsername} autoComplete="username" name="username" required autoFocus invalid={!!error} describedBy={error ? "auth-error" : undefined} />

      {mode === "register" && (
        <>
          <Field label="Email" id="auth-email" type="email" value={email} onChange={(v) => setEmail?.(v)} autoComplete="email" name="email" required />
          <Field label="Display name" id="auth-display-name" value={displayName} onChange={(v) => setDisplayName?.(v)} autoComplete="name" name="display_name" optional />
        </>
      )}

      <Field label="Password" id="auth-password" type="password" value={password} onChange={setPassword} autoComplete={mode === "login" ? "current-password" : "new-password"} name="password" required minLength={mode === "register" ? 8 : undefined} invalid={!!error} describedBy={error ? "auth-error" : undefined} />

      {error && (
        <Alert variant="destructive" id="auth-error">
          {error}
        </Alert>
      )}

      <Button type="submit" loading={loading} size="lg" className="w-full mt-1">
        {loading ? (
          <span>{mode === "login" ? "Signing in…" : "Creating account…"}</span>
        ) : (
          <><span>{mode === "login" ? "Sign in" : "Create account"}</span><ArrowRight className="h-4 w-4" aria-hidden /></>
        )}
      </Button>

      {mode === "login" && (
        <div className="text-center pt-1">
          <Link to="/auth/forgot" className="text-sm text-link hover:text-link-hover hover:underline transition-token">Forgot password?</Link>
        </div>
      )}
    </form>
  );
}

function Field({
  label, id, value, onChange, type = "text", autoComplete, name, required, optional,
  minLength, autoFocus, invalid, describedBy,
}: {
  label: string;
  id: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  autoComplete?: string;
  name?: string;
  required?: boolean;
  optional?: boolean;
  minLength?: number;
  autoFocus?: boolean;
  invalid?: boolean;
  describedBy?: string;
}) {
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 flex items-center gap-2 text-sm font-medium text-foreground">
        {label}
        {optional && <span className="text-xs font-normal text-foreground-muted">optional</span>}
      </label>
      <Input
        id={id}
        name={name}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        required={required}
        minLength={minLength}
        autoComplete={autoComplete}
        autoFocus={autoFocus}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
      />
    </div>
  );
}
