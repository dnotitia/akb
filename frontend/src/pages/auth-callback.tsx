import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { Logo } from "@/components/logo";

/**
 * Retired client-side callback. Ordinary SSO now completes on the fixed
 * server callback and redirects with only an opaque AKB cookie. This route
 * never redeems a code, stores a credential, or marks an SSO identity.
 */
export default function AuthCallbackPage() {
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
