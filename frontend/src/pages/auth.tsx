import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, Database, Boxes, Eye, EyeOff, GitBranch } from "lucide-react";
import {
  authLogin,
  authRegister,
  clearLegacySsoSession,
  getAuthConfig,
  getMe,
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
import { AuthCardLoading } from "@/components/auth-card-loading";
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
  // Each tab keeps its own draft: a half-typed registration password must not
  // become the sign-in password.
  const [login, setLogin] = useState<LoginDraft>(EMPTY_LOGIN);
  const [register, setRegister] = useState<RegisterDraft>(EMPTY_REGISTER);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [message, setMessage] = useState<FormMessage | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);
  // The SSO callback is reached by a browser following a redirect, so it cannot
  // answer with an error body — whatever it returns IS the page. It sends the
  // person back here with a reason instead, and this is where that reason
  // becomes a sentence. Unknown values fall through to the generic line rather
  // than being echoed, so the query string cannot put text on the screen.
  const lifecycleReason = new URLSearchParams(window.location.search).get("reason");
  const lifecycleNotice = lifecycleReason === "sso-sessions-revoked" ? "Your AKB browser sessions have been signed out. Your identity provider session and personal access tokens remain active. You can sign in again with SSO; your account and Vault data are preserved." : lifecycleReason === "sessions-revoked" ? "Your local login sessions have been signed out. Personal access tokens remain active." : lifecycleReason === "account-deleted" ? "Your account deletion request completed." : lifecycleReason === "session-unverified" ? "Sign in again to verify your account. The previous action could not be confirmed." : null;
  const ssoError = new URLSearchParams(window.location.search).get("sso_error") ?? "";
  const [loading, setLoading] = useState(false);
  // Unknown until the versioned public policy is validated. No UI capability
  // is inferred while loading or when the fetch/schema fails.
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [configError, setConfigError] = useState("");
  const [configAttempt, setConfigAttempt] = useState(0);
  const next = safeNext(new URLSearchParams(window.location.search).get("next"));
  const localAuthEnabled =
    authConfig?.available === true &&
    (authConfig.auth_mode === "local" || authConfig.auth_mode === "hybrid") &&
    authConfig.local_auth.enabled;
  const ssoConfigEnabled =
    authConfig?.available === true &&
    (authConfig.auth_mode === "sso" || authConfig.auth_mode === "hybrid") &&
    authConfig.keycloak.enabled;
  const ssoProviders = ssoConfigEnabled ? authConfig.providers : [];
  const usableSsoProviders =
    ssoConfigEnabled &&
    (authConfig.schema_version === 1 || authConfig.keycloak.browser_session_ready)
      ? ssoProviders.filter((provider) => provider.login_url !== null)
      : [];

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        setConfigError("");
        const config = await getAuthConfig();
        if (cancelled) return;
        if (config.available !== true || config.auth_mode === null) {
          setAuthConfig(config);
          return;
        }
        if (config.auth_mode === "sso" && getToken()) {
          // Defense in depth for mocked/legacy config clients: a local JWT must
          // never shadow the SSO cookie carrier.
          setToken(null);
        }
        const hasSessionCandidate = config.auth_mode === "sso" || getToken() !== null;
        if (hasSessionCandidate) {
          try {
            await getMe({ redirectOnUnauthorized: false });
            if (!cancelled) navigate(next, { replace: true });
            return;
          } catch {
            // No current session: reveal only the options allowed by config.
          }
        }
        if (!cancelled) setAuthConfig(config);
      } catch (caught) {
        if (!cancelled) {
          setConfigError(
            caught instanceof Error ? caught.message : "Authentication options could not be loaded.",
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [configAttempt, navigate, next]);

  function startSso(loginUrl: string | null) {
    if (!loginUrl) return;
    window.location.href = `${loginUrl}?redirect=${encodeURIComponent(next)}`;
  }

  function focusField(id: string) {
    requestAnimationFrame(() => {
      const field = formRef.current?.querySelector<HTMLInputElement>(`#${id}`);
      field?.focus();
    });
  }

  function switchMode(next: Mode) {
    setMode(next);
    setFieldErrors({});
    setMessage(null);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (localAuthEnabled !== true) return;
    setMessage(null);
    const errors = mode === "login" ? validateLogin(login) : validateRegister(register);
    setFieldErrors(errors);
    const firstInvalid = FIELD_ORDER.find((field) => errors[field]);
    if (firstInvalid) {
      focusField(FIELD_IDS[firstInvalid]);
      return;
    }
    const username = mode === "login" ? login.username : register.username;
    const password = mode === "login" ? login.password : register.password;
    setLoading(true);
    try {
      if (mode === "register") {
        const reg = await authRegister(username, register.email, password, register.displayName || undefined);
        if (reg.error) {
          setMessage({ kind: "error", text: reg.error });
          return;
        }
      }
      const r = await authLogin(username, password);
      if (r.error || !r.token) {
        // A token-less 200 would set an empty token and bounce back here. And
        // after a successful register, an auto-login failure would strand the
        // user on the register tab (a retry hits the duplicate-account guard).
        if (mode === "register") {
          setRegister(EMPTY_REGISTER);
          setLogin({ username, password: "" });
          setMode("login");
          setMessage({ kind: "info", text: "Account created. Sign in to continue." });
        } else {
          setLogin((draft) => ({ ...draft, password: "" }));
          setMessage({ kind: "error", text: r.error || "Sign-in failed. No session was returned." });
        }
        focusField(FIELD_IDS.password);
        return;
      }
      setToken(r.token);
      clearLegacySsoSession();
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
      setMessage({ kind: "error", text: err?.message || "Something went wrong. Please try again." });
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="relative flex min-h-screen justify-center overflow-clip bg-background text-foreground px-4 pb-6 pt-16 sm:p-6 sm:pt-[12vh]">
      <div className="absolute right-4 top-4 z-10">
        <ThemeToggle />
      </div>

      <main className="relative w-full max-w-5xl grid lg:grid-cols-2 gap-10 lg:gap-16 items-start fade-up">
        {/* Page heading for small screens, where the visual hero (and its h1)
            is display:none — keeps every breakpoint with exactly one h1. */}
        <h1 className="sr-only lg:hidden">AKB — the base your agents remember</h1>
        {/* LEFT — brand hero */}
        <section className="hidden lg:flex flex-col gap-8 pr-4 lg:pt-8">
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
            {lifecycleNotice && <Alert variant="info" className="mb-5">{lifecycleNotice}</Alert>}
            {ssoError && (
              <Alert variant="destructive" id="auth-sso-error" className="mb-5">
                {ssoError === "membership_required"
                  ? "You signed in, but you are not a member of this workspace yet. An administrator has to admit you — they can see that you arrived."
                  : "Sign-in through your identity provider did not complete. Try again, and tell your administrator if it keeps happening."}
              </Alert>
            )}

            {authConfig === null && !configError && <AuthCardLoading label="Loading sign-in options" />}

            {configError && (
              <div className="space-y-4">
                <Alert variant="destructive" title="Sign-in options unavailable">
                  {configError}
                </Alert>
                <Button type="button" variant="outline" className="w-full" onClick={() => setConfigAttempt((value) => value + 1)}>
                  Try again
                </Button>
              </div>
            )}

            {localAuthEnabled && (
              <>
                <h2 className="font-display text-xl font-semibold tracking-tight text-foreground">Welcome to AKB</h2>
                <p className="mt-1 mb-5 text-sm text-foreground-muted">Sign in or create an account to continue.</p>
                <Tabs value={mode} onValueChange={(v) => switchMode(v as Mode)}>
                  <TabsList className="grid w-full grid-cols-2">
                    <TabsTrigger value="login" className="min-h-11 justify-center sm:min-h-9">Sign in</TabsTrigger>
                    <TabsTrigger value="register" className="min-h-11 justify-center sm:min-h-9">Create account</TabsTrigger>
                  </TabsList>
                  <TabsContent value="login" className="pt-5">
                    <AuthForm
                      mode="login" formRef={formRef} values={login}
                      onChange={(field, value) => setLogin((draft) => ({ ...draft, [field]: value }))}
                      fieldErrors={fieldErrors} message={message} loading={loading} onSubmit={handleSubmit}
                    />
                  </TabsContent>
                  <TabsContent value="register" className="pt-5">
                    <AuthForm
                      mode="register" formRef={formRef} values={register}
                      onChange={(field, value) => setRegister((draft) => ({ ...draft, [field]: value }))}
                      onBlurConfirm={() => setFieldErrors((current) => ({ ...current, confirm: confirmError(register) }))}
                      fieldErrors={fieldErrors} message={message} loading={loading} onSubmit={handleSubmit}
                    />
                  </TabsContent>
                </Tabs>
              </>
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
              <div
                className={cn(
                  "space-y-3",
                  localAuthEnabled && "mt-6 border-t border-border pt-6",
                )}
              >
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

          <p className="mt-5 text-center text-xs text-foreground-muted">© Dnotitia</p>
        </section>
      </main>
    </div>
  );
}

type LoginDraft = { username: string; password: string };
type RegisterDraft = LoginDraft & { email: string; displayName: string; confirm: string };
type FieldName = "username" | "email" | "displayName" | "password" | "confirm";
type FieldErrors = Partial<Record<FieldName, string>>;
type FormMessage = { kind: "error" | "info"; text: string };

const EMPTY_LOGIN: LoginDraft = { username: "", password: "" };
const EMPTY_REGISTER: RegisterDraft = { username: "", email: "", displayName: "", password: "", confirm: "" };
const FIELD_ORDER: FieldName[] = ["username", "email", "displayName", "password", "confirm"];
const FIELD_IDS: Record<FieldName, string> = {
  username: "auth-username",
  email: "auth-email",
  displayName: "auth-display-name",
  password: "auth-password",
  confirm: "auth-confirm-password",
};
const MIN_PASSWORD_LENGTH = 8;

function confirmError(draft: RegisterDraft): string | undefined {
  if (!draft.confirm) return undefined;
  return draft.confirm === draft.password ? undefined : "Doesn't match the password.";
}

function validateLogin(draft: LoginDraft): FieldErrors {
  return {
    username: draft.username.trim() ? undefined : "Enter your username.",
    password: draft.password ? undefined : "Enter your password.",
  };
}

function validateRegister(draft: RegisterDraft): FieldErrors {
  return {
    username: draft.username.trim() ? undefined : "Enter a username.",
    email: !draft.email.trim()
      ? "Enter your email address."
      : /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(draft.email.trim()) ? undefined : "Enter a valid email address.",
    password: draft.password.length >= MIN_PASSWORD_LENGTH ? undefined : `Use at least ${MIN_PASSWORD_LENGTH} characters.`,
    confirm: draft.confirm ? confirmError(draft) : "Re-enter your password.",
  };
}

interface AuthFormProps {
  mode: Mode;
  formRef: React.RefObject<HTMLFormElement | null>;
  values: LoginDraft | RegisterDraft;
  onChange: (field: FieldName, value: string) => void;
  onBlurConfirm?: () => void;
  fieldErrors: FieldErrors;
  message: FormMessage | null;
  loading: boolean;
  onSubmit: (e: React.FormEvent) => void;
}

function AuthForm({ mode, formRef, values, onChange, onBlurConfirm, fieldErrors, message, loading, onSubmit }: AuthFormProps) {
  const [showPassword, setShowPassword] = useState(false);
  const register = mode === "register" ? (values as RegisterDraft) : null;
  const passwordType = showPassword ? "text" : "password";
  const field = (name: FieldName) => ({
    id: FIELD_IDS[name],
    value: (values as Record<string, string>)[name] ?? "",
    onChange: (value: string) => onChange(name, value),
    error: fieldErrors[name],
  });
  return (
    <form ref={formRef} onSubmit={onSubmit} noValidate className="space-y-4">
      {message?.kind === "info" && <Alert variant="info" id="auth-message">{message.text}</Alert>}

      <Field label="Username" {...field("username")} autoComplete="username" name="username" autoFocus />

      {register && (
        <>
          <Field label="Email" {...field("email")} type="email" autoComplete="email" name="email" />
          <Field label="Display name" {...field("displayName")} autoComplete="name" name="display_name" optional />
        </>
      )}

      <Field
        label="Password" {...field("password")} type={passwordType} name="password"
        autoComplete={register ? "new-password" : "current-password"}
        hint={register ? `Use at least ${MIN_PASSWORD_LENGTH} characters.` : undefined}
        trailing={
          <button
            type="button"
            onClick={() => setShowPassword((shown) => !shown)}
            aria-label={showPassword ? "Hide password" : "Show password"}
            aria-pressed={showPassword}
            className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-[var(--radius-md)] text-foreground-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {showPassword ? <EyeOff className="h-4 w-4" aria-hidden /> : <Eye className="h-4 w-4" aria-hidden />}
          </button>
        }
      />

      {register && (
        <Field
          label="Confirm password" {...field("confirm")} type={passwordType} name="confirm_password"
          autoComplete="new-password" onBlur={onBlurConfirm}
        />
      )}

      {message?.kind === "error" && (
        <Alert variant="destructive" id="auth-message">
          {message.text}
        </Alert>
      )}

      <Button type="submit" loading={loading} size="lg" className="w-full mt-1">
        {loading ? (
          <span>{register ? "Creating account…" : "Signing in…"}</span>
        ) : (
          <><span>{register ? "Create account" : "Sign in"}</span><ArrowRight className="h-4 w-4" aria-hidden /></>
        )}
      </Button>

      {!register && (
        <div className="text-center pt-1">
          <Link to="/auth/forgot" className="text-sm text-link hover:text-link-hover hover:underline transition-token">Forgot password?</Link>
        </div>
      )}
    </form>
  );
}

function Field({
  label, id, value, onChange, type = "text", autoComplete, name, optional,
  autoFocus, error, hint, trailing, onBlur,
}: {
  label: string;
  id: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  autoComplete?: string;
  name?: string;
  optional?: boolean;
  autoFocus?: boolean;
  error?: string;
  hint?: string;
  trailing?: React.ReactNode;
  onBlur?: () => void;
}) {
  const noteId = `${id}-note`;
  const note = error ?? hint;
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 flex items-center gap-2 text-sm font-medium text-foreground">
        {label}
        {optional && <span className="text-xs font-normal text-foreground-muted">optional</span>}
      </label>
      <div className="relative">
        <Input
          id={id}
          name={name}
          type={type}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onBlur={onBlur}
          autoComplete={autoComplete}
          autoFocus={autoFocus}
          aria-invalid={error ? true : undefined}
          aria-describedby={note ? noteId : undefined}
          // 16px on phones keeps iOS from zooming the page on focus.
          className={cn("h-11 text-base sm:h-10 sm:text-sm", trailing && "pr-11")}
        />
        {trailing}
      </div>
      {note && (
        <p id={noteId} className={cn("mt-1.5 text-xs", error ? "text-destructive" : "text-foreground-muted")}>
          {note}
        </p>
      )}
    </div>
  );
}
