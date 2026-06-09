import { useEffect, useState } from "react";
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
    <div className="min-h-screen flex items-center justify-center bg-surface px-6">
      <div className="w-full max-w-md fade-up">
        <div className="coord mb-3">§ AKB · PUBLIC · RESTRICTED</div>
        <div className="border border-border p-8 relative">
          <h1 className="font-display-tight text-5xl leading-none">
            <span className="text-foreground">Sealed.</span>
            <span className="text-accent italic block mt-1">Pass to open.</span>
          </h1>
          <p className="mt-6 text-sm text-foreground-muted leading-relaxed">
            This publication is protected by a password. Enter it below to read what's inside.
          </p>

          <form onSubmit={submit} className="mt-6 space-y-3">
            <Label htmlFor="pub-gate-pw" className="sr-only">
              Password
            </Label>
            <Input
              id="pub-gate-pw"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="passphrase"
              autoFocus
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? "pub-gate-err" : undefined}
              className="font-mono"
            />
            {error && (
              <Alert variant="destructive" id="pub-gate-err">
                {error}
              </Alert>
            )}
            <Button type="submit" loading={loading} disabled={!password} className="w-full">
              unlock
            </Button>
          </form>
        </div>
        <div className="mt-3 flex justify-between items-center coord">
          <span>SLUG · {slug}</span>
          <a
            href="/"
            className="hover:text-link rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            ← AKB.HOME
          </a>
        </div>
      </div>
    </div>
  );
}
