import { useEffect, useState } from "react";
import { ArrowLeft, Lock } from "lucide-react";
import { submitPublicationPassword } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface Props {
  slug: string;
  onSuccess: () => void;
}

export function PasswordGate({ slug, onSuccess }: Props) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  // Neutral tab title — never echo the slug/title here: a sealed
  // publication's existence/subject must not leak through the document title.
  useEffect(() => {
    const prev = document.title;
    document.title = "Protected publication · AKB";
    return () => {
      document.title = prev;
    };
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await submitPublicationPassword(slug, password);
      onSuccess();
    } catch (err: any) {
      setError(
        err?.message ||
          "That password didn't match. Check with whoever shared this link.",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-6">
      {/* soft teal aurora wash behind the card */}
      <div
        className="pointer-events-none fixed inset-0 -z-10 opacity-60"
        style={{
          background:
            "radial-gradient(60rem 40rem at 50% -10%, color-mix(in oklab, var(--color-primary) 10%, transparent), transparent)",
        }}
        aria-hidden
      />
      <div className="w-full max-w-md fade-up">
        <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-md p-8 text-center">
          <span
            className="inline-flex h-12 w-12 items-center justify-center rounded-[var(--radius-lg)] bg-surface-selected text-primary mx-auto"
            aria-hidden
          >
            <Lock className="h-5 w-5" />
          </span>
          <h1 className="mt-5 font-display text-2xl font-semibold tracking-tight text-foreground">
            Protected publication
          </h1>
          <p className="mt-2 text-sm text-foreground-muted leading-relaxed">
            Enter the password shared with you to read what&rsquo;s inside.
          </p>

          <form onSubmit={submit} className="mt-6 space-y-3 text-left">
            <Label htmlFor="pub-gate-pw" className="sr-only">
              Password
            </Label>
            <Input
              id="pub-gate-pw"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Password"
              autoFocus
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? "pub-gate-err" : undefined}
            />
            {error && (
              <Alert variant="destructive" id="pub-gate-err">
                {error}
              </Alert>
            )}
            <Button type="submit" loading={loading} disabled={!password} className="w-full">
              {loading ? "Unlocking…" : "Unlock"}
            </Button>
          </form>
        </div>
        <div className="mt-4 text-center">
          <a
            href="/"
            className="inline-flex items-center gap-1.5 text-sm text-foreground-muted hover:text-link rounded-[var(--radius-sm)] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
            Back to AKB
          </a>
        </div>
      </div>
    </div>
  );
}
