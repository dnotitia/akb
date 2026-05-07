import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, Loader2 } from "lucide-react";
import { authLogin, authRegister, setToken } from "@/lib/api";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { ThemeToggle } from "@/components/theme-toggle";
import { cn } from "@/lib/utils";

declare const PasswordCredential: {
  new (data: { id: string; password: string }): Credential;
};

type Mode = "login" | "register";

/** Reveal text character-by-character; respects prefers-reduced-motion.
 *  Strict-mode safe: lets effect re-run on remount, cleanup cancels stray intervals. */
function useTyped(text: string, speedMs = 55, start = true) {
  const [shown, setShown] = useState("");

  useEffect(() => {
    if (!start) {
      setShown("");
      return;
    }
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
      setShown(text);
      return;
    }
    setShown("");
    let i = 0;
    const id = window.setInterval(() => {
      i += 1;
      setShown(text.slice(0, i));
      if (i >= text.length) window.clearInterval(id);
    }, speedMs);
    return () => window.clearInterval(id);
  }, [start, text, speedMs]);

  return { shown, done: shown.length >= text.length };
}

/** Loop: type → hold → erase → pause → retype. Respects prefers-reduced-motion. */
function useTypingLoop(
  text: string,
  {
    typeSpeedMs = 40,
    eraseSpeedMs = 22,
    holdMs = 2600,
    pauseMs = 450,
    start = true,
  }: {
    typeSpeedMs?: number;
    eraseSpeedMs?: number;
    holdMs?: number;
    pauseMs?: number;
    start?: boolean;
  } = {},
) {
  const [shown, setShown] = useState("");

  useEffect(() => {
    if (!start) {
      setShown("");
      return;
    }
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
      setShown(text);
      return;
    }

    let timer: number | undefined;
    let charIdx = 0;
    let phase: "type" | "hold" | "erase" | "pause" = "type";
    setShown("");

    const step = () => {
      if (phase === "type") {
        charIdx += 1;
        setShown(text.slice(0, charIdx));
        if (charIdx >= text.length) {
          phase = "hold";
          timer = window.setTimeout(step, holdMs);
        } else {
          timer = window.setTimeout(step, typeSpeedMs);
        }
      } else if (phase === "hold") {
        phase = "erase";
        timer = window.setTimeout(step, eraseSpeedMs);
      } else if (phase === "erase") {
        charIdx -= 1;
        setShown(text.slice(0, Math.max(0, charIdx)));
        if (charIdx <= 0) {
          phase = "pause";
          timer = window.setTimeout(step, pauseMs);
        } else {
          timer = window.setTimeout(step, eraseSpeedMs);
        }
      } else {
        charIdx = 0;
        phase = "type";
        timer = window.setTimeout(step, typeSpeedMs);
      }
    };

    timer = window.setTimeout(step, 0);
    return () => {
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [start, text, typeSpeedMs, eraseSpeedMs, holdMs, pauseMs]);

  return shown;
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

  const title = useTyped("Agent Knowledgebase", 60);
  const subText =
    "A unified base for AI agents — documents, tables, and files under one structured root.";
  const sub = useTypingLoop(subText, {
    start: title.done,
    typeSpeedMs: 22,
    eraseSpeedMs: 14,
    holdMs: 2800,
    pauseMs: 500,
  });
  const subTyping = title.done && sub !== subText;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (mode === "register") {
        const r = await authRegister(
          username,
          email,
          password,
          displayName || undefined,
        );
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
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background text-foreground">
      <style>{`
        @keyframes tw-marquee {
          from { transform: translateX(0); }
          to   { transform: translateX(-50%); }
        }
        .tw-marquee-track {
          display: inline-flex;
          white-space: nowrap;
          animation: tw-marquee 90s linear infinite;
          will-change: transform;
        }
        @media (prefers-reduced-motion: reduce) {
          .tw-marquee-track { animation: none; }
        }
        @keyframes tw-blink { 0%, 49% { opacity: 1 } 50%, 100% { opacity: 0 } }
        .tw-caret {
          display: inline-block;
          width: 0.14em;
          height: 0.95em;
          vertical-align: -0.1em;
          background: currentColor;
          margin-left: 0.12em;
          animation: tw-blink 1.05s steps(1, end) infinite;
        }
        .tw-paper {
          background-image: radial-gradient(
            color-mix(in srgb, var(--color-foreground) 7%, transparent) 1px,
            transparent 1px
          );
          background-size: 4px 4px;
        }
        .tw-rule-dashed {
          background-image: linear-gradient(
            to right,
            color-mix(in srgb, var(--color-foreground) 35%, transparent) 50%,
            transparent 50%
          );
          background-size: 6px 1px;
          background-repeat: repeat-x;
        }
        .tw-key {
          transition: transform 90ms ease-out, box-shadow 90ms ease-out;
          box-shadow: 0 3px 0 0 var(--color-foreground);
        }
        .tw-key:hover:not(:disabled) {
          transform: translateY(-1px);
          box-shadow: 0 4px 0 0 var(--color-foreground);
        }
        .tw-key:active:not(:disabled) {
          transform: translateY(2px);
          box-shadow: 0 1px 0 0 var(--color-foreground);
        }
        .tw-field-input {
          font-family: var(--font-mono);
          caret-color: var(--color-accent);
          letter-spacing: 0.01em;
        }
        .tw-field-input::selection {
          background: var(--color-accent);
          color: var(--color-accent-foreground);
        }
      `}</style>

      {/* Paper grain layer */}
      <div
        className="tw-paper pointer-events-none absolute inset-0 opacity-60"
        aria-hidden
      />

      {/* Top marquee band — editorial strip carrying the masthead */}
      <div
        className="pointer-events-none absolute inset-x-0 top-0 overflow-hidden border-b border-foreground/15 bg-background/50 backdrop-blur-[1px]"
        aria-hidden
      >
        <div className="tw-marquee-track py-1.5 coord">
          {Array.from({ length: 16 }).map((_, i) => (
            <span key={i} className="px-6">
              § AKB · A knowledgebase for agents · Documents / Tables / Files · MCP · Git · Dnotitia · Seahorse ·
            </span>
          ))}
        </div>
      </div>

      {/* Theme toggle — placed below the marquee band to avoid overlap */}
      <div className="absolute right-4 top-10 z-10">
        <ThemeToggle />
      </div>

      <main className="relative mx-auto w-full max-w-5xl px-6 py-12 sm:px-10 sm:py-14 lg:px-12 lg:py-14 fade-up">
        {/* Red typewriter margin rule — always on the far-left paper edge */}
        <div
          className="absolute bottom-10 left-2 top-10 w-px bg-accent/80 sm:left-6 lg:bottom-12 lg:left-8 lg:top-12"
          aria-hidden
        />

        {/* Top sheet rule — mirrors footer's border-t, restores paper's top edge */}
        <div
          className="mb-10 h-px w-full bg-foreground/20 lg:mb-14"
          aria-hidden
        />

        {/* Body — stack on mobile, 2-col split on desktop */}
        <div className="flex flex-col gap-10 lg:grid lg:grid-cols-2 lg:items-start lg:gap-14">
          {/* LEFT — typewriter stage */}
          <section className="flex flex-col lg:pr-2">
            {/* Section marker — matches app convention (§ SETTINGS, § CONNECT…) */}
            <div className="coord-spark mb-3">§ AKB · Entry</div>
            <h1 className="font-mono text-2xl font-semibold leading-[1.1] tracking-tight text-foreground sm:text-[30px] lg:text-[36px]">
              {title.shown}
              {!title.done && <span className="tw-caret" aria-hidden />}
            </h1>

            {/* Editorial tagline — Fraunces display (borrowed from original /auth) */}
            <div
              className={cn(
                "font-display-tight mt-7 leading-[0.92] tracking-tight text-foreground",
                "text-4xl sm:text-[44px] lg:text-[54px]",
              )}
            >
              <div>The base</div>
              <div className="italic">your agents</div>
              <div className="italic text-accent">remember.</div>
            </div>

            {/* Looping description — mono */}
            <p className="mt-8 min-h-[4.5em] font-mono text-sm leading-relaxed text-foreground-muted lg:min-h-[4em] lg:text-[15px]">
              {sub}
              {subTyping && <span className="tw-caret" aria-hidden />}
            </p>
          </section>

          {/* RIGHT — form panel (dashed vertical divider on desktop) */}
          <section className="relative lg:border-l lg:border-dashed lg:border-foreground/30 lg:pl-12">
            {/* Mobile-only horizontal dashed rule above tabs */}
            <div className="tw-rule-dashed mb-8 h-px lg:hidden" aria-hidden />

            <Tabs
              value={mode}
              onValueChange={(v) => {
                setMode(v as Mode);
                setError("");
              }}
            >
              <TabsList className="mb-4 grid w-full grid-cols-2">
                <TabsTrigger value="login" className="justify-center gap-1.5">
                  <span className="coord tabular-nums">01</span>
                  Log in
                </TabsTrigger>
                <TabsTrigger value="register" className="justify-center gap-1.5">
                  <span className="coord tabular-nums">02</span>
                  Register
                </TabsTrigger>
              </TabsList>

              <TabsContent value="login">
                <AuthForm
                  mode="login"
                  username={username}
                  setUsername={setUsername}
                  password={password}
                  setPassword={setPassword}
                  error={error}
                  loading={loading}
                  onSubmit={handleSubmit}
                />
              </TabsContent>
              <TabsContent value="register">
                <AuthForm
                  mode="register"
                  username={username}
                  setUsername={setUsername}
                  email={email}
                  setEmail={setEmail}
                  displayName={displayName}
                  setDisplayName={setDisplayName}
                  password={password}
                  setPassword={setPassword}
                  error={error}
                  loading={loading}
                  onSubmit={handleSubmit}
                />
              </TabsContent>
            </Tabs>
          </section>
        </div>

        {/* Shared footer (full width) */}
        <footer className="mt-12 flex flex-col gap-1 border-t border-foreground/20 pt-4 font-mono text-[10px] uppercase tracking-[0.18em] text-foreground-muted sm:flex-row sm:items-center sm:justify-between sm:gap-0 lg:mt-10">
          <span className="whitespace-nowrap">Sheet 01 / 01</span>
          <span className="whitespace-nowrap">Dnotitia · Seahorse · v1.0</span>
        </footer>
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
  username,
  setUsername,
  email = "",
  setEmail,
  displayName = "",
  setDisplayName,
  password,
  setPassword,
  error,
  loading,
  onSubmit,
}: AuthFormProps) {
  return (
    <form
      onSubmit={onSubmit}
      method="post"
      action="/api/v1/auth/login"
      className="mt-8 space-y-6"
    >
      <Field
        label="Username"
        id="auth-username"
        value={username}
        onChange={setUsername}
        autoComplete="username"
        name="username"
        required
      />

      {mode === "register" && (
        <>
          <Field
            label="Email"
            id="auth-email"
            type="email"
            value={email}
            onChange={(v) => setEmail?.(v)}
            autoComplete="email"
            name="email"
            required
          />
          <Field
            label="Display"
            id="auth-display-name"
            value={displayName}
            onChange={(v) => setDisplayName?.(v)}
            autoComplete="name"
            name="display_name"
            optional
          />
        </>
      )}

      <Field
        label="Password"
        id="auth-password"
        type="password"
        value={password}
        onChange={setPassword}
        autoComplete={mode === "login" ? "current-password" : "new-password"}
        name="password"
        required
      />

      {error && (
        <div
          role="alert"
          aria-live="polite"
          className="border border-destructive px-3 py-2 coord text-destructive"
        >
          ⚠ {error}
        </div>
      )}

      <button
        type="submit"
        disabled={loading}
        className={cn(
          "tw-key mt-2 inline-flex h-12 w-full items-center justify-center gap-3",
          "border border-foreground bg-background",
          "font-mono text-sm font-semibold tracking-tight text-foreground",
          "disabled:cursor-wait disabled:opacity-60",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        )}
      >
        {loading ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            <span>Signing in…</span>
          </>
        ) : (
          <>
            <span>
              {mode === "login" ? "Enter the Base" : "Create Account"}
            </span>
            <ArrowRight className="h-4 w-4 text-accent" aria-hidden />
          </>
        )}
      </button>
    </form>
  );
}

function Field({
  label,
  id,
  value,
  onChange,
  type = "text",
  autoComplete,
  name,
  required,
  optional,
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
      <label htmlFor={id} className="coord mb-1.5 block">
        {label}
        {optional && (
          <span className="ml-2 normal-case tracking-normal text-foreground/40">
            (optional)
          </span>
        )}
      </label>
      <input
        id={id}
        name={name}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        required={required}
        autoComplete={autoComplete}
        className={cn(
          "tw-field-input w-full bg-transparent px-0 py-2.5 text-base text-foreground",
          "border-0 border-b border-dashed border-foreground/40 outline-none",
          "focus:border-solid focus:border-foreground",
          "transition-colors duration-150",
        )}
      />
    </div>
  );
}
