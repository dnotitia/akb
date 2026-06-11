import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { keycloakExchange, markSsoSession, setToken } from "@/lib/api";
import { Logo } from "@/components/logo";

/**
 * Keycloak SSO landing page. The backend callback redirects the browser
 * here with a one-time `code` (and a same-site `redirect` path). We POST
 * the code to exchange it for an AKB JWT — the token is delivered in the
 * response body, never in the URL — store it, then navigate on.
 *
 * On any failure (incl. a hung exchange) we bounce back to /auth with a
 * reason so the user sees a readable message instead of a dead-end.
 */
export default function AuthCallbackPage() {
  const navigate = useNavigate();
  // StrictMode double-invokes effects in dev; the code is single-use, so
  // guard against a second redeem that would always 400.
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    document.title = "Signing in… — AKB";

    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    const rawRedirect = params.get("redirect") || "/";
    // Same-site only: resolve against our origin and keep just path+query+hash
    // when it stays same-origin (URL parsing also defuses %2F / whitespace
    // tricks that a string `includes` check would miss).
    let safeRedirect = "/";
    try {
      const u = new URL(rawRedirect, window.location.origin);
      if (u.origin === window.location.origin) safeRedirect = u.pathname + u.search + u.hash;
    } catch {
      /* malformed → home */
    }

    if (!code) {
      navigate("/auth?sso_error=missing_code", { replace: true });
      return;
    }

    // Cap the exchange so a hung backend doesn't spin the user forever.
    let timer: ReturnType<typeof setTimeout>;
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error("timeout")), 15000);
    });
    Promise.race([keycloakExchange(code), timeout])
      .then((r) => {
        if (r.error || !r.token) {
          navigate("/auth?sso_error=exchange_failed", { replace: true });
          return;
        }
        setToken(r.token);
        // Mark this as an SSO session so Sign out also ends the KC session.
        markSsoSession(r.kc_id_token);
        navigate(safeRedirect, { replace: true });
      })
      .catch((e: unknown) => {
        const reason = e instanceof Error && e.message === "timeout" ? "timeout" : "exchange_failed";
        navigate(`/auth?sso_error=${reason}`, { replace: true });
      })
      .finally(() => clearTimeout(timer));
  }, [navigate]);

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background text-foreground p-6">
      <div className="hero-glow w-full max-w-md mx-auto fade-up flex flex-col items-center gap-8">
        <h1 className="sr-only">Completing sign-in</h1>
        <Logo size={40} subtitle />
        <div className="flex flex-col items-center gap-4 coord" role="status" aria-live="polite">
          <Loader2 className="h-6 w-6 animate-spin text-primary" aria-hidden />
          <span>Completing sign-in…</span>
        </div>
      </div>
    </div>
  );
}
