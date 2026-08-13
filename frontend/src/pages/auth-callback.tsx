import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { Logo } from "@/components/logo";

/**
 * Phase 1 fail-closed landing page. Phase 4 will replace this with a
 * server-custodied browser session; this component never redeems a code,
 * stores a credential, or marks a client-side SSO identity.
 */
export default function AuthCallbackPage() {
  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background text-foreground p-6">
      <div className="hero-glow w-full max-w-md mx-auto fade-up flex flex-col items-center gap-8 text-center">
        <Logo size={40} subtitle />
        <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-lg p-7 sm:p-8">
          <h1 className="font-display text-2xl tracking-tight text-foreground mb-4">
            SSO sign-in unavailable
          </h1>
          <p className="text-sm text-foreground-muted leading-relaxed mb-6">
            Browser SSO sessions are not available in this release. No sign-in
            code or credential was used.
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
