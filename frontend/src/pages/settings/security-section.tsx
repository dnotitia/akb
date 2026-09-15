import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { ShieldCheck, TriangleAlert } from "lucide-react";
import { ApiError, authSessionSnapshot, beginAccountLifecycle, clearCompletedAccountSession, isCurrentAuthSession, type AuthSessionSnapshot } from "@/lib/api";
import { deleteAccount, getAccountLifecycle, getDeletionBlockers, revokeAccountSessions, type AccountLifecycle, type DeletionBlockers } from "@/lib/api-account-lifecycle";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Panel, PanelHeader } from "@/components/ui/panel";
import type { User } from "./profile-section";

function reasonText(reason: string | null | undefined): string {
  switch (reason) {
    case "managed_account": return "Your organization manages this account through SSO. Contact your organization or Keycloak administrator for account changes.";
    case "recovery_admin_protected": return "This recovery administrator account cannot be deleted.";
    case "human_session_required": return "Sign in through your account's browser login to manage account security.";
    case "local_auth_disabled": return "Local account management is disabled by your organization's sign-in policy.";
    case "credential_change_required": return "Change your issued password before managing account security.";
    case "cleanup_unavailable": return "Account deletion is temporarily unavailable. Try again later.";
    default: return "This server does not currently support this action for your account.";
  }
}
function blockerText(code: string, count?: number) {
  if (code === "owned_vaults") return `You own ${count === undefined ? "one or more" : count} vault${count === 1 ? "" : "s"}. Transfer ownership or delete each vault separately first.`;
  if (code === "last_active_admin" || code === "last_local_admin" || code === "last_admin") return "Another active local administrator is required before you can delete this account.";
  return reasonText(code);
}
function failureText(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Your session could not be verified. Sign in again. This does not confirm whether the action completed.";
    if (error.status === 404 || error.status === 405) return "This server does not support this action. Your sign-in information has been kept.";
    if (error.status === 409) {
      const detail = error.detail as { details?: { blockers?: { code?: unknown; count?: unknown }[] } } | null;
      const blockers = detail?.details?.blockers;
      const reasons = Array.isArray(blockers) ? blockers.filter(b => typeof b?.code === "string")
        .map(b => blockerText(b.code as string, typeof b.count === "number" ? b.count : undefined)) : [];
      return [error.message, ...reasons].join(" ");
    }
    if (error.status === 403) return error.message;
    if (error.status === 429) return "Too many attempts. Wait before checking your account and trying again.";
  }
  return "We could not verify the result. Check your account status before trying again; the action may have completed.";
}

type Preview = { data: AccountLifecycle; snapshot: AuthSessionSnapshot };
export function SecuritySection({ user, onBusyChange }: { user: User; onBusyChange: (busy: boolean) => void }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [vaults, setVaults] = useState<DeletionBlockers | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState("");
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const [step, setStep] = useState<"none" | "sessions" | "review" | "delete">("none");
  const [password, setPassword] = useState("");
  const returnFocus = useRef<HTMLElement | null>(null);
  const mounted = useRef(true);
  const readSequence = useRef(0);
  const failedSession = useRef<AuthSessionSnapshot | null>(null);
  const setWorking = (value: boolean) => { busyRef.current = value; setBusy(value); onBusyChange(value); };

  async function refresh(openReview = false) {
    const sequence = ++readSequence.current;
    const snapshot = authSessionSnapshot();
    setLoading(true); setPreview(null); setVaults(null); setNeedsSignIn(false);
    try {
      const data = await getAccountLifecycle(snapshot);
      if (!mounted.current || sequence !== readSequence.current) return;
      if (!isCurrentAuthSession(snapshot) || data.user_id !== user.user_id || data.username !== user.username) {
        throw new Error("identity_changed");
      }
      setPreview({ data, snapshot });
      if (openReview) setStep("review");
      if (data.deletion.supported && data.deletion.blockers.some(b => b.code === "owned_vaults")) {
        const page = await getDeletionBlockers(snapshot);
        if (mounted.current && sequence === readSequence.current && isCurrentAuthSession(snapshot) && page.user_id === user.user_id) setVaults(page);
      }
    } catch (e) {
      if (mounted.current && sequence === readSequence.current) {
        setError(e instanceof Error && e.message === "identity_changed" ? "Your signed-in account changed. Reload Settings and review the action again." : failureText(e));
        failedSession.current = snapshot;
        setNeedsSignIn(e instanceof ApiError && e.status === 401);
      }
    } finally { if (mounted.current && sequence === readSequence.current) setLoading(false); }
  }
  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => { mounted.current = false; readSequence.current += 1; onBusyChange(false); };
    // Each identity mounts a new section. Reads never automatically retry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user.user_id]);
  useEffect(() => {
    const invalidate = () => {
      if (preview && !isCurrentAuthSession(preview.snapshot)) {
        setPassword(""); setStep("none"); setPreview(null);
        setError("Your sign-in session changed. Check your account status and review the action again.");
      }
    };
    window.addEventListener("storage", invalidate);
    window.addEventListener("focus", invalidate);
    return () => { window.removeEventListener("storage", invalidate); window.removeEventListener("focus", invalidate); };
  }, [preview]);

  const ssoSession = authSessionSnapshot().mode === "sso" || user.auth_method === "browser_session";
  const managedAccount = ssoSession || preview?.data.deletion.reason === "managed_account";
  const canRevoke = !!preview && preview.data.revoke_sessions.supported &&
    preview.data.revoke_sessions.scope === (preview.snapshot.mode === "sso" ? "sso_browser_sessions" : "local_sessions") &&
    preview.data.revoke_sessions.includes_current && !preview.data.revoke_sessions.affects_pats;
  const sessionDescription = ssoSession
    ? "Sign out all your AKB browser sessions, including this device. You can sign in again with SSO."
    : "Sign out all supported local login sessions, including this device. You will need to sign in again.";
  const sessionExclusions = ssoSession
    ? "Your identity provider (SSO) session and personal access tokens (PATs) remain active. Your account and Vault data are preserved."
    : "Personal access tokens (PATs) and agent connections remain active.";
  const canDelete = !managedAccount && !!preview && preview.data.deletion.supported && preview.data.deletion.allowed && preview.data.deletion.blockers.length === 0 && preview.data.deletion.confirmation === "username_and_current_password";
  function close() { if (!busyRef.current) { setStep("none"); setPassword(""); } }
  async function execute(kind: "sessions" | "delete") {
    if (busyRef.current || !preview || (kind === "sessions" ? !canRevoke : !canDelete || !password)) return;
    const { snapshot, data } = preview;
    if (!isCurrentAuthSession(snapshot)) {
      setPassword(""); setStep("none"); setPreview(null);
      setError("Your sign-in session changed. Check your account status and review the action again.");
      return;
    }
    setWorking(true); setError("");
    let end: (() => void) | undefined;
    let completed = false;
    try {
      end = beginAccountLifecycle(snapshot);
      if (kind === "sessions") await revokeAccountSessions(snapshot, data.user_id);
      else await deleteAccount(snapshot, data.user_id, data.username, password);
      if (!isCurrentAuthSession(snapshot)) return;
      await queryClient.cancelQueries();
      if (!isCurrentAuthSession(snapshot)) return;
      queryClient.clear();
      if (clearCompletedAccountSession(snapshot)) {
        completed = true;
        const reason = kind === "delete" ? "account-deleted" : data.revoke_sessions.scope === "sso_browser_sessions" ? "sso-sessions-revoked" : "sessions-revoked";
        navigate(`/auth?reason=${reason}`, { replace: true });
      }
    } catch (e) {
      if (!mounted.current || !isCurrentAuthSession(snapshot)) return;
      setError(failureText(e));
      failedSession.current = snapshot;
      setNeedsSignIn(e instanceof ApiError && e.status === 401);
      setStep(kind === "delete" ? "review" : "none");
      // Every failure requires a new preview and explicit confirmation; no mutation retries.
      setPreview(null);
    } finally {
      end?.();
      if (mounted.current) {
        setPassword(""); setWorking(false);
        if (!completed && !isCurrentAuthSession(snapshot)) {
          setPreview(null); setStep("none");
          setError("Your sign-in session changed. Check your account status and review the action again.");
        }
      }
    }
  }
  async function signInAgain() {
    const snapshot = failedSession.current;
    if (!snapshot || !isCurrentAuthSession(snapshot)) {
      setNeedsSignIn(false);
      setError("Your sign-in session changed. Check your account status again.");
      return;
    }
    setWorking(true);
    try {
      await queryClient.cancelQueries();
      if (!isCurrentAuthSession(snapshot)) return;
      queryClient.clear();
      if (clearCompletedAccountSession(snapshot)) navigate("/auth?reason=session-unverified", { replace: true });
    } finally { if (mounted.current) setWorking(false); }
  }
  async function moreVaults() {
    if (!preview || !vaults?.next_cursor || busyRef.current) return;
    setWorking(true);
    try {
      const page = await getDeletionBlockers(preview.snapshot, vaults.next_cursor);
      if (isCurrentAuthSession(preview.snapshot) && page.user_id === user.user_id) setVaults({ ...page, owned_vaults: [...vaults.owned_vaults, ...page.owned_vaults] });
    } catch (e) { setError(failureText(e)); }
    finally { if (mounted.current) setWorking(false); }
  }
  const feedback = error && <Alert variant="destructive">{error}</Alert>;
  const effects = preview?.data.deletion.effects;
  return <section className="max-w-3xl space-y-6" aria-labelledby="security-title" aria-busy={busy || loading}>
    <div><h2 id="security-title" className="flex items-center gap-2 text-lg font-semibold"><ShieldCheck className="h-5 w-5 text-link" aria-hidden />Security</h2><p className="mt-1 text-sm text-foreground-muted">Manage your login sessions and account.</p></div>
    {step !== "review" && step !== "delete" && feedback}
    {loading && <p role="status" className="text-sm text-foreground-muted">Checking account security…</p>}
    {busy && <p role="status" className="text-sm text-foreground-muted">Working… Keep this page open while we verify the result.</p>}
    {!loading && !preview && <div className="flex flex-wrap gap-2"><Button variant="outline" disabled={busy} onClick={() => { setError(""); void refresh(); }}>Check account status</Button>{needsSignIn && <Button variant="outline" disabled={busy} onClick={() => void signInAgain()}>Sign in again</Button>}</div>}
    <Panel><PanelHeader label="Login sessions" /><div className="space-y-3 p-4 text-sm"><p>{sessionDescription}</p><p className="text-foreground-muted">{sessionExclusions}</p>{preview && !canRevoke && <Alert variant="info">{reasonText(preview.data.revoke_sessions.reason)}</Alert>}<Button variant="outline" disabled={busy || loading || !canRevoke} onClick={() => { returnFocus.current = document.activeElement as HTMLElement; setError(""); setStep("sessions"); }}>Sign out all sessions</Button></div></Panel>
    {managedAccount ? <Panel><PanelHeader label="Managed account" /><div className="space-y-3 p-4 text-sm"><p>{reasonText("managed_account")}</p><p className="text-foreground-muted">Signing out keeps your account and Vault data available for your next SSO sign-in.</p></div></Panel> : <Panel><PanelHeader label={<span className="flex items-center gap-2 text-destructive"><TriangleAlert className="h-4 w-4" aria-hidden />Danger zone</span>} /><div className="space-y-3 p-4 text-sm"><h3 className="font-semibold">Delete account</h3><p>This permanently deletes your account and personal access tokens. This cannot be undone. Shared vault content and publication links remain available.</p>{preview && !preview.data.deletion.supported && <Alert variant="info">{reasonText(preview.data.deletion.reason)}</Alert>}{preview?.data.deletion.blockers.map(b => <Alert key={b.code} variant="warning">{blockerText(b.code, b.count)}</Alert>)}{vaults && <ul className="space-y-2">{vaults.owned_vaults.map(v => <li key={v.id} className="break-words"><Link className="text-link hover:underline" to={`/vault/${encodeURIComponent(v.name)}/settings`}>{v.name} — manage ownership</Link></li>)}</ul>}{vaults?.next_cursor && <Button variant="outline" disabled={busy} onClick={() => void moreVaults()}>Show more owned vaults</Button>}<Button variant="destructive" disabled={busy || loading || !preview?.data.deletion.supported} onClick={() => { returnFocus.current = document.activeElement as HTMLElement; setError(""); setPassword(""); void refresh(true); }}>Review account deletion</Button></div></Panel>}
    <ConfirmDialog open={step === "sessions"} onOpenChange={open => { if (!open) close(); }} returnFocusRef={returnFocus} title="Sign out all sessions?" description={`${sessionDescription} ${sessionExclusions}`} variant="destructive" confirmLabel="Sign out all sessions" busy={busy} onConfirm={() => execute("sessions")} />
    <Dialog open={step === "review"} onOpenChange={open => { if (!open) close(); }}><DialogContent onCloseAutoFocus={event => { if (step !== "delete") { event.preventDefault(); returnFocus.current?.focus(); } }}><DialogHeader><DialogTitle>Review account deletion</DialogTitle><DialogDescription>Review the impact on {user.username} before continuing. This action cannot be undone.</DialogDescription></DialogHeader><div className="space-y-3 text-sm">{feedback}{needsSignIn && <Button variant="outline" disabled={busy} onClick={() => void signInAgain()}>Sign in again</Button>}{preview && <><p>Personal access tokens to delete: {effects?.active_pats_revoked ?? "Unavailable"}</p><p>Shared publication links preserved: {effects?.shared_publications_preserved ?? "Unavailable"}</p>{preview.data.deletion.blockers.map(b => <Alert key={b.code} variant="warning">{blockerText(b.code, b.count)}</Alert>)}</>}{!preview && <Button variant="outline" disabled={loading || busy} onClick={() => { setError(""); void refresh(); }}>Check account status</Button>}<Label htmlFor="account-current-password">Current password</Label><Input id="account-current-password" type="password" autoComplete="current-password" value={password} disabled={busy || loading || !canDelete} onChange={e => setPassword(e.target.value)} /></div><DialogFooter><Button variant="outline" onClick={close} disabled={busy} autoFocus>Cancel</Button><Button variant="destructive" disabled={busy || loading || !canDelete || !password} onClick={() => setStep("delete")}>Continue to confirmation</Button></DialogFooter></DialogContent></Dialog>
    <ConfirmDialog open={step === "delete"} onOpenChange={open => { if (!open && !busyRef.current) { setStep(current => current === "delete" ? "none" : current); setPassword(""); } }} returnFocusRef={returnFocus} title="Permanently delete account?" description="Your account and personal access tokens will be deleted. Shared vault content and publication links will be preserved." variant="destructive" confirmationText={user.username} confirmationLabel="Type your username to confirm" confirmLabel="Permanently delete account" busy={busy} onConfirm={() => execute("delete")} />
  </section>;
}
