import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { ThemeToggle } from "@/components/theme-toggle";
import { Logo } from "@/components/logo";

export default function AuthForgotPage() {
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
          <div className="coord-spark mb-2">§ FORGOT PASSWORD</div>
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
          <Link
            to="/auth"
            className="inline-flex items-center gap-1.5 coord hover:text-accent transition-colors"
          >
            <ArrowLeft className="h-3 w-3" aria-hidden />
            BACK TO LOGIN
          </Link>
        </div>

        <p className="mt-5 text-center coord">Dnotitia · Seahorse · v1.0</p>
      </main>
    </div>
  );
}
