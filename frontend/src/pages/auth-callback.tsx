import { useEffect, useRef, useState } from "react";
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
 * On any failure we bounce back to /auth with a reason so the user sees a
 * readable message instead of a dead-end.
 */
export default function AuthCallbackPage() {
  const navigate = useNavigate();
  const [error, setError] = useState("");
  // StrictMode double-invokes effects in dev; the code is single-use, so
  // guard against a second redeem that would always 400.
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;

    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    const redirect = params.get("redirect") || "/";
    // Same-site path only — mirror the backend _safe_redirect_path guard
    // (block scheme-relative "//" and backslash tricks).
    const safeRedirect =
      redirect.startsWith("/") && !redirect.startsWith("//") && !redirect.includes("\\")
        ? redirect
        : "/";

    if (!code) {
      navigate("/auth?sso_error=missing_code", { replace: true });
      return;
    }

    keycloakExchange(code)
      .then((r) => {
        if (r.error || !r.token) {
          setError(r.error || "Exchange failed");
          navigate("/auth?sso_error=exchange_failed", { replace: true });
          return;
        }
        setToken(r.token);
        // Mark this as an SSO session so Sign out also ends the KC session.
        markSsoSession(r.kc_id_token);
        navigate(safeRedirect, { replace: true });
      })
      .catch(() => {
        navigate("/auth?sso_error=exchange_failed", { replace: true });
      });
  }, [navigate]);

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background text-foreground p-6">
      <div className="hero-glow w-full max-w-md mx-auto fade-up flex flex-col items-center gap-8">
        <Logo size={40} subtitle />
        <div className="flex flex-col items-center gap-4 coord">
          {error ? (
            <span>Redirecting…</span>
          ) : (
            <>
              <Loader2 className="h-6 w-6 animate-spin text-accent" aria-hidden />
              <span>Completing sign-in…</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
