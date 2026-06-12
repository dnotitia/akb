import { useEffect, useState } from "react";
import { changePassword, updateProfile } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RoleBadge } from "@/components/status-badge";
import { useFlashStatus } from "@/hooks/use-flash-status";

export interface User {
  user_id: string;
  username: string;
  email: string;
  display_name?: string;
  is_admin?: boolean;
}

interface Props {
  user: User;
  onUserUpdate: (patch: { display_name?: string; email?: string }) => void;
}

export function ProfileSection({ user, onUserUpdate }: Props) {
  const [profileDisplayName, setProfileDisplayName] = useState("");
  const [profileEmail, setProfileEmail] = useState("");
  const [profileError, setProfileError] = useState("");
  // Benign "nothing to save" message — kept off the red error channel so a
  // no-op submit doesn't read as a failure.
  const [profileNotice, setProfileNotice] = useState("");
  const [profileBusy, setProfileBusy] = useState(false);
  const profileFlash = useFlashStatus(3000);

  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwConfirm, setPwConfirm] = useState("");
  const [pwError, setPwError] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwTouched, setPwTouched] = useState({ new: false, confirm: false });
  const passwordFlash = useFlashStatus(3000);

  const pwTooShort = pwTouched.new && pwNew.length > 0 && pwNew.length < 8;
  const pwMismatch = pwTouched.confirm && pwConfirm.length > 0 && pwNew !== pwConfirm;
  const pwSubmitDisabled =
    pwBusy || pwNew.length < 8 || pwNew !== pwConfirm || pwCurrent.length === 0;

  // Sync local edit state when user payload arrives.
  useEffect(() => {
    setProfileDisplayName(user.display_name ?? "");
    setProfileEmail(user.email ?? "");
  }, [user]);

  // Guard unsaved profile edits behind the browser's unload prompt (refresh /
  // close / external nav). In-app SPA nav has the Save/notice as its net.
  useEffect(() => {
    const dirty =
      (user.display_name ?? "") !== profileDisplayName || user.email !== profileEmail;
    if (!dirty || profileBusy) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [user, profileDisplayName, profileEmail, profileBusy]);

  async function handleSaveProfile(e: React.FormEvent) {
    e.preventDefault();
    setProfileError("");
    setProfileNotice("");
    const patch: { display_name?: string; email?: string } = {};
    if ((user.display_name ?? "") !== profileDisplayName) patch.display_name = profileDisplayName;
    if (user.email !== profileEmail) patch.email = profileEmail;
    if (!Object.keys(patch).length) {
      setProfileNotice("No changes to save");
      return;
    }
    setProfileBusy(true);
    try {
      const res = await updateProfile(patch);
      onUserUpdate({ display_name: res.display_name ?? undefined, email: res.email });
      profileFlash.setFlash("Saved");
    } catch (err) {
      setProfileError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setProfileBusy(false);
    }
  }

  async function handleChangePassword(e: React.FormEvent) {
    e.preventDefault();
    setPwError("");
    if (pwNew !== pwConfirm) {
      setPwError("New password and confirmation do not match");
      return;
    }
    if (pwNew.length < 8) {
      setPwError("New password must be at least 8 characters");
      return;
    }
    setPwBusy(true);
    try {
      await changePassword(pwCurrent, pwNew);
      passwordFlash.setFlash("Password changed");
      setPwCurrent("");
      setPwNew("");
      setPwConfirm("");
      setPwTouched({ new: false, confirm: false });
    } catch (e) {
      setPwError(e instanceof Error ? e.message : "Failed to change password");
    } finally {
      setPwBusy(false);
    }
  }

  const profileDirty =
    (user.display_name ?? "") !== profileDisplayName || user.email !== profileEmail;

  return (
    <>
      {/* Account card */}
      <form
        onSubmit={handleSaveProfile}
        className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden"
      >
        <header className="border-b border-border px-6 py-3">
          <span className="coord-ink">Account</span>
        </header>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-4 p-6">
          <ReadOnlyField label="Username" value={user.username} />
          <div>
            <div className="coord mb-1">Role</div>
            <RoleBadge role={user.is_admin ? "admin" : "user"} />
          </div>
          <div>
            <Label htmlFor="profile-display-name">Display name</Label>
            <Input
              id="profile-display-name"
              value={profileDisplayName}
              onChange={(e) => setProfileDisplayName(e.target.value)}
              placeholder="—"
            />
          </div>
          <div>
            <Label htmlFor="profile-email">Email</Label>
            <Input
              id="profile-email"
              type="email"
              value={profileEmail}
              onChange={(e) => setProfileEmail(e.target.value)}
              required
            />
          </div>
        </div>
        <div className="flex items-center gap-3 px-6 pb-6 flex-wrap">
          <Button type="submit" loading={profileBusy} disabled={!profileDirty}>
            Save profile
          </Button>
          {profileFlash.message && (
            <span role="status" aria-live="polite" className="text-sm text-success">
              {profileFlash.message}
            </span>
          )}
          {profileNotice && (
            <span role="status" aria-live="polite" className="text-sm text-foreground-muted">
              {profileNotice}
            </span>
          )}
          {profileError && (
            <span role="alert" className="text-sm text-destructive">
              {profileError}
            </span>
          )}
        </div>
      </form>

      {/* Change password card */}
      <section
        className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden"
        aria-labelledby="change-pw-heading"
      >
        <header className="border-b border-border px-6 py-3">
          <span id="change-pw-heading" className="coord-ink">Change password</span>
        </header>
        <form onSubmit={handleChangePassword} className="space-y-3 p-6 max-w-md">
          <div>
            <Label htmlFor="pw-current">Current password</Label>
            <Input
              id="pw-current"
              type="password"
              autoComplete="current-password"
              value={pwCurrent}
              onChange={(e) => setPwCurrent(e.target.value)}
              required
            />
          </div>
          <div>
            <Label htmlFor="pw-new">New password</Label>
            <Input
              id="pw-new"
              type="password"
              autoComplete="new-password"
              value={pwNew}
              onChange={(e) => setPwNew(e.target.value)}
              onBlur={() => setPwTouched((t) => ({ ...t, new: true }))}
              aria-invalid={pwTooShort || undefined}
              aria-describedby={pwTooShort ? "pw-new-help" : undefined}
              required
            />
            {pwTooShort && (
              <p id="pw-new-help" className="text-destructive text-xs mt-1">
                Use at least 8 characters.
              </p>
            )}
          </div>
          <div>
            <Label htmlFor="pw-confirm">Confirm new password</Label>
            <Input
              id="pw-confirm"
              type="password"
              autoComplete="new-password"
              value={pwConfirm}
              onChange={(e) => setPwConfirm(e.target.value)}
              onBlur={() => setPwTouched((t) => ({ ...t, confirm: true }))}
              aria-invalid={pwMismatch || undefined}
              aria-describedby={pwMismatch ? "pw-confirm-help" : undefined}
              required
            />
            {pwMismatch && (
              <p id="pw-confirm-help" className="text-destructive text-xs mt-1">
                Doesn&apos;t match new password.
              </p>
            )}
          </div>
          {pwError && (
            <p role="alert" className="text-destructive text-xs">
              {pwError}
            </p>
          )}
          {passwordFlash.message && (
            <p role="status" aria-live="polite" className="text-success text-xs">
              {passwordFlash.message}
            </p>
          )}
          <Button type="submit" loading={pwBusy} disabled={pwSubmitDisabled} aria-disabled={pwSubmitDisabled}>
            Change password
          </Button>
        </form>
      </section>
    </>
  );
}

function ReadOnlyField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="coord mb-1">{label}</div>
      <div className="text-sm font-medium text-foreground">{value}</div>
    </div>
  );
}
