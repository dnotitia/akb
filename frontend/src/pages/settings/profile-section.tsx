import { Activity, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { authSessionSnapshot, beginAccountLifecycle, isCurrentAuthSession, clearCompletedAccountSession, changePassword, getMe, updateProfile, type CurrentUser } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert } from "@/components/ui/alert";
import { useFlashStatus } from "@/hooks/use-flash-status";
import { ProfilePhotoEditor, useProfilePhotoEditor } from "./profile-photo-editor";

export type User = Pick<CurrentUser, "user_id" | "username" | "email" | "display_name" | "is_admin">
  & Partial<Pick<CurrentUser, "auth_method" | "key_class" | "account_kind" | "avatar" | "profile_capabilities">>;
export type ProfileUserPatch = Partial<Pick<CurrentUser, "display_name" | "email" | "avatar" | "profile_capabilities">>;

interface Props {
  user: User;
  localPasswordEnabled: boolean;
  localProfileEditingEnabled: boolean;
  onUserUpdate: (patch: ProfileUserPatch) => void;
  onDirtyChange?: (dirty: boolean) => void;
  onBusyChange?: (busy: boolean) => void;
  onPasswordChanged?: () => void;
  visible?: boolean;
}

export function ProfileSection({
  user,
  localPasswordEnabled,
  localProfileEditingEnabled,
  onUserUpdate,
  onDirtyChange,
  onBusyChange,
  onPasswordChanged,
  visible = true,
}: Props) {
  const mounted = useRef(true);
  const owner = useRef(user.user_id);
  owner.current = user.user_id;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [profileDisplayName, setProfileDisplayName] = useState(
    (user.display_name ?? "").trim(),
  );
  const [profileEmail, setProfileEmail] = useState((user.email ?? "").trim());
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
  const [currentPasswordError, setCurrentPasswordError] = useState(false);
  const currentPasswordInput = useRef<HTMLInputElement>(null);
  const [pwBusy, setPwBusy] = useState(false);
  const [pwTouched, setPwTouched] = useState({ new: false, confirm: false });
  const passwordFlash = useFlashStatus(3000);
  const photo = useProfilePhotoEditor({ user, visible, blocked: profileBusy || pwBusy, onUserUpdate });

  const pwTooShort = pwTouched.new && pwNew.length > 0 && pwNew.length < 8;
  const pwMismatch = pwTouched.confirm && pwConfirm.length > 0 && pwNew !== pwConfirm;
  const pwSubmitDisabled =
    pwBusy || photo.busy || pwNew.length < 8 || pwNew !== pwConfirm || pwCurrent.length === 0;

  const baseline = useRef({ id: user.user_id, name: (user.display_name ?? "").trim(), email: (user.email ?? "").trim() });
  // Same-account identity refreshes must not replace an active profile draft.
  useEffect(() => {
    const previous = baseline.current;
    setProfileDisplayName(value => previous.id !== user.user_id || value.trim() === previous.name ? (user.display_name ?? "").trim() : value);
    setProfileEmail(value => previous.id !== user.user_id || value.trim() === previous.email ? (user.email ?? "").trim() : value);
    baseline.current = { id: user.user_id, name: (user.display_name ?? "").trim(), email: (user.email ?? "").trim() };
  }, [user.user_id, user.display_name, user.email]);
  useEffect(() => { if (visible && !pwBusy && currentPasswordError) currentPasswordInput.current?.focus(); }, [visible, pwBusy, currentPasswordError]);

  const profileDirty =
    (user.display_name ?? "").trim() !== profileDisplayName.trim()
    || (user.email ?? "").trim() !== profileEmail.trim();
  const dirty = (localProfileEditingEnabled && profileDirty)
    || (localPasswordEnabled && Boolean(pwCurrent || pwNew || pwConfirm)) || photo.dirty;

  useLayoutEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useLayoutEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);
  const busy = profileBusy || pwBusy || photo.busy;
  useLayoutEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useLayoutEffect(() => () => onBusyChange?.(false), [onBusyChange]);

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
    if (!visible || !localProfileEditingEnabled || profileBusy || photo.busy) return;
    setProfileError("");
    setProfileNotice("");
    const patch: { display_name?: string; email?: string } = {};
    const displayName = profileDisplayName.trim();
    if ((user.display_name ?? "") !== displayName) patch.display_name = displayName;
    if (user.email !== profileEmail.trim()) patch.email = profileEmail.trim();
    if (!Object.keys(patch).length) {
      setProfileDisplayName((user.display_name ?? "").trim());
      setProfileEmail((user.email ?? "").trim());
      setProfileNotice("No changes to save");
      return;
    }
    setProfileBusy(true);
    const submittedOwner = user.user_id;
    const session = authSessionSnapshot();
    try {
      const res = await updateProfile(patch);
      if (!mounted.current || owner.current !== submittedOwner || !isCurrentAuthSession(session)) return;
      setProfileDisplayName(res.display_name ?? "");
      setProfileEmail(res.email);
      onUserUpdate({ display_name: res.display_name, email: res.email });
      profileFlash.setFlash("Saved");
    } catch (err) {
      if (mounted.current && owner.current === submittedOwner && isCurrentAuthSession(session)) setProfileError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      // Activity can suspend effects while retaining this state. Completion
      // must release its busy flag even while hidden (a true unmount ignores it).
      setProfileBusy(false);
    }
  }

  async function handleChangePassword(e: React.FormEvent) {
    e.preventDefault();
    if (!visible || !localPasswordEnabled || pwBusy || photo.busy) return;
    setPwError("");
    setCurrentPasswordError(false);
    if (pwNew !== pwConfirm) {
      setPwError("New password and confirmation do not match");
      return;
    }
    if (pwNew.length < 8) {
      setPwError("New password must be at least 8 characters");
      return;
    }
    setPwBusy(true);
    const submittedOwner = user.user_id;
    const session = authSessionSnapshot();
    let endLifecycle: (() => void) | undefined;
    try {
      // A foreground proof can observe this mutation's revoked generation
      // before its receipt arrives. Let the explicit action own that transition.
      endLifecycle = beginAccountLifecycle(session);
      await changePassword(pwCurrent, pwNew);
      if (!mounted.current || owner.current !== submittedOwner || !isCurrentAuthSession(session)) return;
      passwordFlash.setFlash("Password changed. Sign in again with your new password.");
      setPwCurrent("");
      setPwNew("");
      setPwConfirm("");
      setPwTouched({ new: false, confirm: false });
      // The backend revokes this local login generation transactionally. Do
      // not clear a newer account/session which replaced it while saving.
      if (clearCompletedAccountSession(session)) onPasswordChanged?.();
    } catch (e) {
      if (e instanceof Error && e.name === "DeferredSessionError" && isCurrentAuthSession(session)) {
        // A failed password request is no longer a pending revocation. Release
        // its suppression and prove expiry normally (legacy servers use 401
        // for both wrong-password and expired-session errors).
        endLifecycle?.(); endLifecycle = undefined;
        try { await getMe(); } catch { return; }
      }
      if (!mounted.current || owner.current !== submittedOwner || !isCurrentAuthSession(session)) return;
      setPwError(e instanceof Error ? e.message : "Failed to change password");
      setCurrentPasswordError(typeof e === "object" && e !== null && "detail" in e &&
        typeof e.detail === "object" && e.detail !== null && "code" in e.detail && e.detail.code === "current_password_incorrect");
    } finally {
      endLifecycle?.();
      setPwBusy(false);
    }
  }

  return (
    <Activity mode={visible ? "visible" : "hidden"}><div className="@container/profile min-w-0 space-y-8">
      <section aria-labelledby="profile-heading">
        <header className="border-b border-border pb-4">
          <h2 id="profile-heading" className="text-base font-semibold text-foreground">Profile</h2>
          <p className="mt-1 text-sm text-foreground-muted">Your display name and AKB account identity.</p>
        </header>

        <div data-testid="profile-identity"><ProfilePhotoEditor editor={photo} /></div>
        <form onSubmit={handleSaveProfile} aria-busy={profileBusy || undefined}>
          <div className="space-y-4">
            <div className="grid min-w-0 gap-2 @min-[28rem]/profile:grid-cols-[9rem_minmax(0,1fr)] @min-[28rem]/profile:gap-4">
              <span className="text-sm font-medium @min-[28rem]/profile:pt-2">Username</span>
              <div>
                <p className="min-h-10 break-all rounded-[var(--radius-md)] border border-border bg-surface-2 px-3 py-2 text-sm text-foreground">
                  @{user.username}
                </p>
                <p className="mt-1.5 text-xs leading-relaxed text-foreground-muted">
                  Used for sign-in and Vault sharing. Your username cannot currently be changed.
                </p>
              </div>
            </div>
            <div className="grid min-w-0 gap-2 @min-[28rem]/profile:grid-cols-[9rem_minmax(0,1fr)] @min-[28rem]/profile:gap-4">
              <Label htmlFor="profile-display-name" className="@min-[28rem]/profile:pt-3">Display name</Label>
              <div>
                <Input
                  id="profile-display-name"
                  autoComplete="name"
                  value={profileDisplayName}
                  onChange={(e) => setProfileDisplayName(e.target.value)}
                  placeholder="—"
                  maxLength={100}
                  aria-describedby="profile-display-name-help"
                  disabled={!localProfileEditingEnabled || profileBusy}
                />
                <p id="profile-display-name-help" className="mt-1.5 text-xs leading-relaxed text-foreground-muted">
                  Shown to collaborators. Display names do not need to be unique.
                </p>
              </div>
            </div>
            <div className="grid min-w-0 gap-2 @min-[28rem]/profile:grid-cols-[9rem_minmax(0,1fr)] @min-[28rem]/profile:gap-4">
              <Label htmlFor="profile-email" className="@min-[28rem]/profile:pt-3">Email address</Label>
              <Input
                id="profile-email"
                type="email"
                autoComplete="email"
                value={profileEmail}
                onChange={(e) => setProfileEmail(e.target.value)}
                required
                disabled={!localProfileEditingEnabled || profileBusy}
              />
            </div>
          </div>

          {localProfileEditingEnabled ? (
            <div className="mt-4 flex flex-wrap items-center gap-3 @min-[28rem]/profile:pl-40">
              <Button type="submit" loading={profileBusy} disabled={!profileDirty || profileBusy || photo.busy}>
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
          <header className="border-b border-border pb-4">
            <h2 id="change-pw-heading" className="text-base font-semibold text-foreground">Change password</h2>
          </header>
          <form onSubmit={handleChangePassword} className="pt-5" aria-busy={pwBusy || undefined}>
            <div className="space-y-4">
              <div className="grid min-w-0 gap-2 @min-[28rem]/profile:grid-cols-[9rem_minmax(0,1fr)] @min-[28rem]/profile:gap-4">
                <Label htmlFor="pw-current" className="@min-[28rem]/profile:pt-3">Current password</Label>
                <Input
                  id="pw-current"
                  ref={currentPasswordInput}
                  type="password"
                  autoComplete="current-password"
                  value={pwCurrent}
                  onChange={(e) => { setPwCurrent(e.target.value); if (currentPasswordError) { setCurrentPasswordError(false); setPwError(""); } }}
                  aria-invalid={currentPasswordError || undefined}
                  aria-describedby={currentPasswordError ? "pw-error" : undefined}
                  required
                  disabled={pwBusy}
                />
              </div>
              <div className="grid min-w-0 gap-2 @min-[28rem]/profile:grid-cols-[9rem_minmax(0,1fr)] @min-[28rem]/profile:gap-4">
                <Label htmlFor="pw-new" className="@min-[28rem]/profile:pt-3">New password</Label>
                <div className="min-w-0">
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
                    disabled={pwBusy}
                  />
                  <p id="pw-new-help" className={`mt-1.5 text-xs ${pwTooShort ? "text-destructive" : "text-foreground-muted"}`}>
                    Use at least 8 characters.
                  </p>
                </div>
              </div>
              <div className="grid min-w-0 gap-2 @min-[28rem]/profile:grid-cols-[9rem_minmax(0,1fr)] @min-[28rem]/profile:gap-4">
                <Label htmlFor="pw-confirm" className="leading-5 @min-[28rem]/profile:pt-2.5">Confirm new password</Label>
                <div className="min-w-0">
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
                    disabled={pwBusy}
                  />
                  {pwMismatch && (
                    <p id="pw-confirm-help" className="mt-1.5 text-xs text-destructive">
                      Doesn&apos;t match new password.
                    </p>
                  )}
                </div>
              </div>
            </div>
            {pwError && (
              <p id="pw-error" role="alert" className="mt-3 text-sm text-destructive @min-[28rem]/profile:pl-40">
                {pwError}
              </p>
            )}
            {passwordFlash.message && (
              <p role="status" aria-live="polite" className="mt-3 text-sm text-success @min-[28rem]/profile:pl-40">
                {passwordFlash.message}
              </p>
            )}
            <div className="mt-4 flex flex-wrap items-center gap-3 @min-[28rem]/profile:pl-40">
              <Button type="submit" loading={pwBusy} disabled={pwSubmitDisabled} aria-disabled={pwSubmitDisabled}>
                Change password
              </Button>
              <span className="flex items-center gap-1.5 text-xs text-foreground-muted">
                <ShieldCheck className="h-4 w-4" aria-hidden />
                Local sign-ins, including this session, will end
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
    </div></Activity>
  );
}
