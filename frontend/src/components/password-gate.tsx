import { useState } from "react";
import { submitPublicationPassword } from "@/lib/api";

interface Props {
  slug: string;
  onSuccess: () => void;
}

export function PasswordGate({ slug, onSuccess }: Props) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await submitPublicationPassword(slug, password);
      onSuccess();
    } catch (err: any) {
      setError(err.message || "Invalid password");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-surface px-6">
      <div className="w-full max-w-md fade-up">
        <div className="coord mb-3">§ AKB · PUBLIC · RESTRICTED</div>
        <div className="border border-border p-8 grain relative">
          <div className="font-display-tight text-5xl text-foreground leading-none">
            Sealed.
          </div>
          <div className="font-display-tight text-5xl text-accent italic leading-none mt-1">
            Pass to open.
          </div>
          <p className="mt-6 text-sm text-foreground-muted leading-relaxed">
            This publication is protected by a password. Enter it below to read what's inside.
          </p>

          <form onSubmit={submit} className="mt-6 space-y-3">
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="passphrase"
              autoFocus
              className="w-full h-11 px-3 rounded-[var(--radius-md)] border border-border bg-transparent font-mono text-sm placeholder:text-foreground-muted placeholder:uppercase placeholder:tracking-wider placeholder:text-xs focus:outline-none focus:border-accent"
            />
            {error && (
              <div className="border border-destructive p-2 coord-spark" style={{ color: "var(--color-destructive)" }}>
                ⚠ {error.toUpperCase()}
              </div>
            )}
            <button
              type="submit"
              disabled={loading || !password}
              className="w-full h-11 rounded-[var(--radius-md)] border border-primary bg-primary text-primary-foreground font-medium tracking-tight shadow-sm hover:bg-primary/90 disabled:opacity-40 transition-token"
            >
              {loading ? "verifying…" : "→ unlock"}
            </button>
          </form>
        </div>
        <div className="mt-3 flex justify-between items-center coord">
          <span>SLUG · {slug}</span>
          <a href="/" className="hover:text-accent">← AKB.HOME</a>
        </div>
      </div>
    </div>
  );
}
