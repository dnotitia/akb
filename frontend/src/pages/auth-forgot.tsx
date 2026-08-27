import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { getAuthConfig, getMe, getToken, type AuthConfig } from "@/lib/api";
import { ThemeToggle } from "@/components/theme-toggle";
import { Logo } from "@/components/logo";
import { AuthCardLoading } from "@/components/auth-card-loading";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

export default function AuthForgotPage() {
  const navigate = useNavigate();
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [configError, setConfigError] = useState("");
  const [configAttempt, setConfigAttempt] = useState(0);
  const localPasswordHelp =
    authConfig?.available === true &&
    (authConfig.auth_mode === "local" || authConfig.auth_mode === "hybrid") &&
    authConfig.local_auth.enabled;
  // This route sits outside the Layout auth gate — bounce signed-in users home.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        setConfigError("");
        const config = await getAuthConfig();
        if (cancelled) return;
        const hasSessionCandidate =
          config.available === true &&
          (config.auth_mode === "sso" || getToken() !== null);
        if (hasSessionCandidate) {
          try {
            await getMe({ redirectOnUnauthorized: false });
            if (!cancelled) navigate("/", { replace: true });
            return;
          } catch {
            // No active session; render mode-specific recovery guidance.
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
  }, [configAttempt, navigate]);

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background text-foreground p-6">
      <div className="absolute right-4 top-4 z-10">
        <ThemeToggle />
      </div>

      <main className="hero-glow w-full max-w-md mx-auto fade-up">
        <div className="mb-8 flex justify-center">
          <Logo size={40} subtitle />
        </div>

        <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-lg p-7 sm:p-8">
          {configError ? (
            <div className="space-y-4">
              <Alert variant="destructive" title="Recovery options unavailable">
                {configError}
              </Alert>
              <Button type="button" variant="outline" className="w-full" onClick={() => setConfigAttempt((value) => value + 1)}>
                Try again
              </Button>
            </div>
          ) : authConfig === null ? (
            <AuthCardLoading label="Loading authentication options" compact />
          ) : localPasswordHelp ? (
            <>
              <h1 className="font-display text-2xl tracking-tight text-foreground mb-4">
                Forgot your password?
              </h1>
              <p className="text-sm text-foreground-muted leading-relaxed mb-3">
                Contact your administrator to reset your password. They will provide
                you with a temporary password you can use to log in.
              </p>
              <p className="text-sm text-foreground-muted leading-relaxed mb-6">
                Once logged in, change it from <strong>Settings → Profile</strong>.
              </p>
            </>
          ) : (
            <>
              <h1 className="font-display text-2xl tracking-tight text-foreground mb-4">
                Password recovery unavailable
              </h1>
              <p className="text-sm text-foreground-muted leading-relaxed mb-6">
                Local password recovery is not available for this authentication mode.
              </p>
            </>
          )}
          <Link
            to="/auth"
            className="inline-flex items-center gap-1.5 text-sm text-link hover:text-link-hover transition-token"
          >
            <ArrowLeft className="h-3 w-3" aria-hidden />
            Back to login
          </Link>
        </div>

        <p className="mt-5 text-center coord">Dnotitia · Seahorse · v1.0</p>
      </main>
    </div>
  );
}
