import { useEffect, useState } from "react";
import { ShieldCheck, UserRound } from "lucide-react";
import { changePassword, updateProfile } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
  onDirtyChange?: (dirty: boolean) => void;
}

export function ProfileSection({
  user,
  localPasswordEnabled,
  localProfileEditingEnabled,
  onUserUpdate,
  onDirtyChange,
}: Props) {
  const [profileDisplayName, setProfileDisplayName] = useState(user.display_name ?? "");
  const [profileEmail, setProfileEmail] = useState(user.email ?? "");
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
  }, [user.user_id, user.display_name, user.email]);

  const profileDirty =
    (user.display_name ?? "") !== profileDisplayName || (user.email ?? "") !== profileEmail;
  const dirty = (localProfileEditingEnabled && profileDirty)
    || (localPasswordEnabled && Boolean(pwCurrent || pwNew || pwConfirm));

  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  // Protect both profile and password work during refresh / external navigation.
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

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
    if (!localPasswordEnabled) return;
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

  return (
    <div className="grid w-full max-w-6xl items-start gap-10 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)] xl:gap-12">
      <section aria-labelledby="profile-heading">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
          <h2 id="profile-heading" className="text-base font-semibold text-foreground">Public profile</h2>
          <div className="flex min-w-0 items-center gap-2 text-xs text-foreground-muted">
            <span className="break-all">@{user.username}</span>
            <RoleBadge role={user.is_admin ? "admin" : "user"} />
          </div>
        </header>

        <div className="my-5 flex items-center gap-4 rounded-[var(--radius-md)] border border-border bg-surface p-4" data-testid="profile-identity">
          <span className="flex h-16 w-16 shrink-0 items-center justify-center rounded-full bg-surface-selected text-xl font-semibold text-surface-selected-foreground" aria-hidden>
            {(user.display_name?.trim() || user.username).slice(0, 2) || <UserRound className="h-6 w-6" />}
          </span>
          <div className="min-w-0">
            <p className="break-words text-base font-semibold">{user.display_name?.trim() || user.username}</p>
            <p className="mt-1 break-all text-sm text-foreground-muted">{user.email}</p>
          </div>
        </div>
        <form onSubmit={handleSaveProfile}>
          <div className="space-y-4">
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
            <div className="mt-5 flex flex-wrap items-center gap-3">
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
      </section>

      {localPasswordEnabled ? (
        <section aria-labelledby="change-pw-heading">
          <header className="border-b border-border pb-3">
            <h2 id="change-pw-heading" className="text-base font-semibold text-foreground">Change password</h2>
          </header>
          <form onSubmit={handleChangePassword} className="pt-5">
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
                  aria-describedby="pw-new-help"
                  required
                />
                <p id="pw-new-help" className={`mt-1 text-xs ${pwTooShort ? "text-destructive" : "text-foreground-muted"}`}>
                  Use at least 8 characters.
                </p>
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
            <div className="mt-5 flex flex-wrap items-center gap-3">
              <Button type="submit" loading={pwBusy} disabled={pwSubmitDisabled} aria-disabled={pwSubmitDisabled}>
                Change password
              </Button>
              <span className="flex items-center gap-1.5 text-xs text-foreground-muted">
                <ShieldCheck className="h-4 w-4" aria-hidden />
                Current session stays active
              </span>
            </div>
          </form>
        </section>
      ) : (
        <section aria-labelledby="managed-access-heading">
          <header className="border-b border-border pb-3">
            <h2 id="managed-access-heading" className="text-base font-semibold text-foreground">Managed access</h2>
          </header>
          <div className="max-w-xl pt-2">
            <dl className="divide-y divide-border">
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
        </section>
      )}
    </div>
  );
}
