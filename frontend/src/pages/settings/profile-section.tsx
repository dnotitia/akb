import { useEffect, useState } from "react";
import { BadgeCheck, LockKeyhole, ShieldCheck, UserRound } from "lucide-react";
import { changePassword, updateProfile } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Panel } from "@/components/ui/panel";
import { Alert } from "@/components/ui/alert";
import { RoleBadge } from "@/components/status-badge";
import { useFlashStatus } from "@/hooks/use-flash-status";

export interface User {
  user_id: string;
  username: string;
  email: string;
  display_name: string | null;
  is_admin: boolean;
  auth_method?: string;
  key_class?: string | null;
}

interface Props {
  user: User;
  localPasswordEnabled: boolean;
  localProfileEditingEnabled: boolean;
  onUserUpdate: (patch: { display_name?: string; email?: string }) => void;
}

export function ProfileSection({
  user,
  localPasswordEnabled,
  localProfileEditingEnabled,
  onUserUpdate,
}: Props) {
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
    if (!localProfileEditingEnabled || !dirty || profileBusy) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [user, profileDisplayName, profileEmail, profileBusy, localProfileEditingEnabled]);

  async function handleSaveProfile(e: React.FormEvent) {
    e.preventDefault();
    if (!localProfileEditingEnabled) return;
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
  const displayLabel = user.display_name?.trim() || user.username;
  const initials = initialsFor(displayLabel);

  return (
    <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1.08fr)_minmax(22rem,0.92fr)]">
      <Panel>
        <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-5 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-surface-selected text-surface-selected-foreground">
              <UserRound className="h-4 w-4" aria-hidden />
            </span>
            <div className="min-w-0">
              <h2 className="text-base font-semibold text-foreground">Public profile</h2>
              <p className="mt-1 text-sm text-foreground-muted">
                Your identity across vaults and activity.
              </p>
            </div>
          </div>
          <RoleBadge role={user.is_admin ? "admin" : "user"} />
        </div>

        <div className="flex items-center gap-4 border-b border-border bg-surface-2/70 px-5 py-4 sm:px-6">
          <div
            className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border border-border-strong bg-surface text-sm font-semibold text-primary shadow-xs"
            aria-hidden
          >
            {initials || <UserRound className="h-5 w-5" />}
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-foreground">{displayLabel}</div>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-foreground-muted">
              <span>@{user.username}</span>
              <span aria-hidden>·</span>
              <span>{user.email}</span>
            </div>
          </div>
        </div>

        <form onSubmit={handleSaveProfile} className="p-5 sm:p-6">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
            <div>
              <Label htmlFor="profile-display-name">Display name</Label>
              <Input
                id="profile-display-name"
                value={profileDisplayName}
                onChange={(e) => setProfileDisplayName(e.target.value)}
                placeholder="—"
                disabled={!localProfileEditingEnabled}
              />
            </div>
            <div>
              <Label htmlFor="profile-email">Email address</Label>
              <Input
                id="profile-email"
                type="email"
                value={profileEmail}
                onChange={(e) => setProfileEmail(e.target.value)}
                required
                disabled={!localProfileEditingEnabled}
              />
            </div>
          </div>

          {localProfileEditingEnabled ? (
            <div className="mt-6 flex flex-wrap items-center gap-3 border-t border-border pt-5">
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
          ) : (
            <Alert variant="info" className="mt-5">
              Profile details are managed by your identity provider.
            </Alert>
          )}
        </form>
      </Panel>

      {localPasswordEnabled ? (
        <Panel aria-labelledby="change-pw-heading">
          <div className="flex items-start gap-3 border-b border-border px-5 py-5 sm:px-6">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-surface-selected text-surface-selected-foreground">
              <LockKeyhole className="h-4 w-4" aria-hidden />
            </span>
            <div>
              <h2 id="change-pw-heading" className="text-base font-semibold text-foreground">Change password</h2>
              <p className="mt-1 text-sm text-foreground-muted">
                Rotate your local sign-in password.
              </p>
            </div>
          </div>
          <form onSubmit={handleChangePassword} className="p-5 sm:p-6">
            <div className="space-y-4">
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
                  <p id="pw-new-help" className="mt-1 text-xs text-destructive">
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
                  <p id="pw-confirm-help" className="mt-1 text-xs text-destructive">
                    Doesn&apos;t match new password.
                  </p>
                )}
              </div>
            </div>
            {pwError && (
              <p role="alert" className="mt-3 text-xs text-destructive">
                {pwError}
              </p>
            )}
            {passwordFlash.message && (
              <p role="status" aria-live="polite" className="mt-3 text-xs text-success">
                {passwordFlash.message}
              </p>
            )}
            <div className="mt-6 flex flex-wrap items-center justify-between gap-4 border-t border-border pt-5">
              <span className="flex items-center gap-1.5 text-xs text-foreground-muted">
                <ShieldCheck className="h-4 w-4" aria-hidden />
                Current session stays active
              </span>
              <Button type="submit" loading={pwBusy} disabled={pwSubmitDisabled} aria-disabled={pwSubmitDisabled}>
                Change password
              </Button>
            </div>
          </form>
        </Panel>
      ) : (
        <Panel aria-labelledby="managed-access-heading">
          <div className="flex items-start gap-3 border-b border-border px-5 py-5 sm:px-6">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-success-soft text-success-soft-foreground">
              <BadgeCheck className="h-4 w-4" aria-hidden />
            </span>
            <div>
              <h2 id="managed-access-heading" className="text-base font-semibold text-foreground">Managed access</h2>
              <p className="mt-1 text-sm text-foreground-muted">
                Your organization controls sign-in security.
              </p>
            </div>
          </div>
          <div className="p-5 sm:p-6">
            <dl className="divide-y divide-border rounded-[var(--radius-md)] border border-border bg-surface-2/50 px-4">
              <div className="flex items-center justify-between gap-4 py-3 text-sm">
                <dt className="text-foreground-muted">Sign-in method</dt>
                <dd className="font-medium text-foreground">Identity provider</dd>
              </div>
              <div className="flex items-center justify-between gap-4 py-3 text-sm">
                <dt className="text-foreground-muted">Password changes</dt>
                <dd className="font-medium text-foreground">Managed externally</dd>
              </div>
            </dl>
            <p className="mt-4 text-xs leading-relaxed text-foreground-muted">
              Contact your workspace administrator if you need to recover or change your sign-in credentials.
            </p>
          </div>
        </Panel>
      )}
    </div>
  );
}

function initialsFor(label: string): string {
  const words = label.trim().split(/\s+/).filter(Boolean);
  if (words.length > 1) {
    return `${Array.from(words[0])[0] ?? ""}${Array.from(words[words.length - 1])[0] ?? ""}`;
  }
  return Array.from(words[0] ?? "?").slice(0, 2).join("");
}
