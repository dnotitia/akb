import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { Logo } from "@/components/logo";
import {
  getAuthConfig,
  keycloakExchange,
  markLegacySsoSession,
  setToken,
} from "@/lib/api";
import { InlineLoadingState } from "@/components/ui/loading-state";

/**
 * Compatibility callback for the exact pre-v2 Keycloak contract. Current v2
 * SSO still completes on the fixed server callback and never redeems a code in
 * the browser; only a validated schema_version=1 hybrid policy can enter the
 * temporary exchange branch below.
 */
export default function AuthCallbackPage() {
  const navigate = useNavigate();
  const ran = useRef(false);
  const [state, setState] = useState<"checking" | "retired">("checking");

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    let cancelled = false;
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    const rawRedirect = params.get("redirect") || "/";
    let redirect = "/";
    try {
      const candidate = new URL(rawRedirect, window.location.origin);
      if (candidate.origin === window.location.origin) {
        redirect = candidate.pathname + candidate.search + candidate.hash;
      }
    } catch {
      // Malformed or cross-origin targets return home.
    }

    void (async () => {
      const config = await getAuthConfig();
      if (cancelled) return;
      if (
        config.available !== true ||
        config.schema_version !== 1 ||
        config.auth_mode !== "hybrid" ||
        !config.keycloak.enabled
      ) {
        setState("retired");
        return;
      }
      if (!code) {
        navigate("/auth?sso_error=missing_code", { replace: true });
        return;
      }

      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const timeout = new Promise<never>((_resolve, reject) => {
          timer = setTimeout(() => reject(new Error("timeout")), 15_000);
        });
        const result = await Promise.race([keycloakExchange(code), timeout]);
        if (cancelled) return;
        const token = result?.token;
        const idToken = result?.kc_id_token;
        const tokenHasControlCharacters =
          typeof token === "string" &&
          Array.from(token).some((character) => {
            const codePoint = character.charCodeAt(0);
            return codePoint < 0x20 || codePoint === 0x7f;
          });
        if (
          result?.error ||
          typeof token !== "string" ||
          token.length === 0 ||
          token.length > 16_384 ||
          tokenHasControlCharacters ||
          (idToken !== undefined && typeof idToken !== "string")
        ) {
          navigate("/auth?sso_error=exchange_failed", { replace: true });
          return;
        }
        setToken(token);
        markLegacySsoSession(idToken);
        navigate(redirect, { replace: true });
      } catch (caught) {
        const reason = caught instanceof Error && caught.message === "timeout"
          ? "timeout"
          : "exchange_failed";
        if (!cancelled) {
          navigate(`/auth?sso_error=${reason}`, { replace: true });
        }
      } finally {
        if (timer !== undefined) clearTimeout(timer);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [navigate]);

  if (state === "checking") {
    return (
      <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background p-6 text-foreground">
        <div className="hero-glow flex w-full max-w-md flex-col items-center gap-8 text-center">
          <Logo size={40} subtitle />
          <div className="w-full rounded-[var(--radius-lg)] border border-border bg-surface p-8 shadow-lg">
            <InlineLoadingState label="Completing sign-in…" className="py-8" />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background text-foreground p-6">
      <div className="hero-glow w-full max-w-md mx-auto fade-up flex flex-col items-center gap-8 text-center">
        <Logo size={40} subtitle />
        <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-lg p-7 sm:p-8">
          <h1 className="font-display text-2xl tracking-tight text-foreground mb-4">
            Legacy SSO callback retired
          </h1>
          <p className="text-sm text-foreground-muted leading-relaxed mb-6">
            AKB now completes SSO through its fixed server callback. Start the
            sign-in flow again; no code or credential on this page was used.
          </p>
          <Link
            to="/auth"
            className="inline-flex items-center gap-1.5 text-sm text-link hover:text-link-hover transition-token"
          >
            <ArrowLeft className="h-3 w-3" aria-hidden />
            Back to sign in
          </Link>
        </div>
      </div>
    </div>
  );
}
