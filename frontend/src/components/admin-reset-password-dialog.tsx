import { useEffect, useState } from "react";
import { Check, Copy, Key } from "lucide-react";
import { adminResetPassword } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface Props {
  userId: string;
  username: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function AdminResetPasswordDialog({ userId, username, open, onOpenChange }: Props) {
  const [working, setWorking] = useState(false);
  const [tempPassword, setTempPassword] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      setTempPassword(null);
      setCopied(false);
      setError("");
      setWorking(false);
    }
  }, [open]);

  async function handleGenerate() {
    setError("");
    setWorking(true);
    try {
      const r = await adminResetPassword(userId);
      setTempPassword(r.temporary_password);
    } catch (e: any) {
      setError(e?.message || "Failed to reset password");
    } finally {
      setWorking(false);
    }
  }

  async function handleCopy() {
    if (!tempPassword) return;
    try {
      // clipboard is undefined on insecure (plain-HTTP) origins — guard with
      // `?.` so a copy never throws an uncaught TypeError with no feedback.
      await navigator.clipboard?.writeText(tempPassword);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard failure is silent — user can re-click / select manually
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !working && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Key className="h-4 w-4" aria-hidden /> Reset password
          </DialogTitle>
          <DialogDescription>
            Generate a temporary password for <strong>{username}</strong>.
          </DialogDescription>
        </DialogHeader>

        {tempPassword === null ? (
          <div className="space-y-3 text-sm">
            <p>
              The user's current password will be replaced immediately. They
              will be able to log in with the generated password and then change
              it from Settings.
            </p>
            {error && <Alert variant="destructive">{error}</Alert>}
          </div>
        ) : (
          <div className="space-y-3" role="status" aria-live="polite">
            <Alert variant="warning">
              Share this with the user out-of-band (Slack, in person, etc.).
              It cannot be retrieved again.
            </Alert>
            <div className="relative">
              <pre
                data-testid="temp-password"
                className="font-mono text-base bg-surface-muted border border-border p-3 select-all break-all"
              >
                {tempPassword}
              </pre>
              <button
                type="button"
                onClick={handleCopy}
                aria-label={copied ? "Temporary password copied" : "Copy temporary password"}
                className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-1 text-[11px] font-mono uppercase tracking-wider text-foreground-muted hover:text-primary border border-border bg-surface rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
              >
                {copied ? <><Check className="h-3 w-3" aria-hidden /> COPIED</> : <><Copy className="h-3 w-3" aria-hidden /> COPY</>}
              </button>
            </div>
          </div>
        )}

        <DialogFooter>
          {tempPassword === null ? (
            <>
              <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={working}>
                Cancel
              </Button>
              <Button onClick={handleGenerate} loading={working}>
                Generate
              </Button>
            </>
          ) : (
            <Button onClick={() => onOpenChange(false)}>Done</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
