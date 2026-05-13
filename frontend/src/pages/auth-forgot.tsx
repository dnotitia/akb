import { Link } from "react-router-dom";

export default function AuthForgotPage() {
  return (
    <div className="max-w-md mx-auto px-4 py-16 fade-up">
      <div className="coord-spark mb-2">§ FORGOT PASSWORD</div>
      <h1 className="text-2xl font-semibold tracking-tight text-foreground mb-4">
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
        className="coord hover:text-accent transition-colors"
      >
        ← BACK TO LOGIN
      </Link>
    </div>
  );
}
