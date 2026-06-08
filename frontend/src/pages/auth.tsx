import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, Loader2, Database, Boxes, GitBranch } from "lucide-react";
import { authLogin, authRegister, clearSsoSession, getAuthConfig, setToken } from "@/lib/api";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ThemeToggle } from "@/components/theme-toggle";
import { Logo } from "@/components/logo";
import { cn } from "@/lib/utils";

declare const PasswordCredential: {
  new (data: { id: string; password: string }): Credential;
};

type Mode = "login" | "register";

export default function AuthPage() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<Mode>("login");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [ssoLoginUrl, setSsoLoginUrl] = useState<string | null>(null);

  // Optional Keycloak SSO: show the button only if the backend reports it
  // enabled. Also surface a friendly message if the callback bounced back.
  useEffect(() => {
    getAuthConfig().then((cfg) => {
      if (cfg.keycloak.enabled && cfg.keycloak.login_url) {
        setSsoLoginUrl(cfg.keycloak.login_url);
      }
    });
    const params = new URLSearchParams(window.location.search);
    const ssoErr = params.get("sso_error");
    if (ssoErr) setError(`SSO login failed (${ssoErr}). Please try again.`);
  }, []);

  function startSso() {
    if (!ssoLoginUrl) return;
    window.location.href = `${ssoLoginUrl}?redirect=${encodeURIComponent("/")}`;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (mode === "register") {
        const r = await authRegister(username, email, password, displayName || undefined);
        if (r.error) {
          setError(r.error);
          setLoading(false);
          return;
        }
      }
      const r = await authLogin(username, password);
      if (r.error) {
        setError(r.error);
        setLoading(false);
        return;
      }
      setToken(r.token);
      clearSsoSession();
      if ("PasswordCredential" in window) {
        const cred = new PasswordCredential({ id: username, password });
        navigator.credentials.store(cred).catch(() => {});
      }
      navigate("/");
    } catch (err: any) {
      setError(err.message);
    }
    setLoading(false);
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background text-foreground p-6">
      <div className="absolute right-4 top-4 z-10">
        <ThemeToggle />
      </div>

      <main className="relative w-full max-w-5xl grid lg:grid-cols-2 gap-10 lg:gap-16 items-center fade-up">
        {/* LEFT — brand hero */}
        <section className="hidden lg:flex flex-col gap-8 pr-4">
          <Logo size={42} subtitle />
          <div>
            <h1 className="font-display text-5xl leading-[1.05] tracking-tight text-foreground">
              The base your<br />agents <span className="brand-gradient">remember.</span>
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
                  <Icon size={17} strokeWidth={1.75} />
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

            {ssoLoginUrl && (
              <div className="mt-6">
                <div className="mb-4 flex items-center gap-3">
                  <div className="h-px flex-1 bg-border" aria-hidden />
                  <span className="coord">or</span>
                  <div className="h-px flex-1 bg-border" aria-hidden />
                </div>
                <Button type="button" variant="outline" size="lg" className="w-full" onClick={startSso}>
                  Sign in with SSO
                  <ArrowRight className="h-4 w-4" aria-hidden />
                </Button>
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
    <form onSubmit={onSubmit} method="post" action="/api/v1/auth/login" className="space-y-4">
      <Field label="Username" id="auth-username" value={username} onChange={setUsername} autoComplete="username" name="username" required />

      {mode === "register" && (
        <>
          <Field label="Email" id="auth-email" type="email" value={email} onChange={(v) => setEmail?.(v)} autoComplete="email" name="email" required />
          <Field label="Display name" id="auth-display-name" value={displayName} onChange={(v) => setDisplayName?.(v)} autoComplete="name" name="display_name" optional />
        </>
      )}

      <Field label="Password" id="auth-password" type="password" value={password} onChange={setPassword} autoComplete={mode === "login" ? "current-password" : "new-password"} name="password" required />

      {error && (
        <div role="alert" aria-live="polite" className="rounded-[var(--radius-md)] border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <Button type="submit" variant="default" size="lg" disabled={loading} className="w-full mt-1">
        {loading ? (
          <><Loader2 className="h-4 w-4 animate-spin" aria-hidden /><span>Signing in…</span></>
        ) : (
          <><span>{mode === "login" ? "Sign in" : "Create account"}</span><ArrowRight className="h-4 w-4" aria-hidden /></>
        )}
      </Button>

      {mode === "login" && (
        <div className="text-center pt-1">
          <Link to="/auth/forgot" className="coord hover:text-accent transition-colors">Forgot password?</Link>
        </div>
      )}
    </form>
  );
}

function Field({
  label, id, value, onChange, type = "text", autoComplete, name, required, optional,
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
}) {
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 flex items-center gap-2 text-sm font-medium text-foreground">
        {label}
        {optional && <span className="coord normal-case">optional</span>}
      </label>
      <Input
        id={id}
        name={name}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        required={required}
        autoComplete={autoComplete}
      />
    </div>
  );
}
