import { Activity, useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, type NavigateOptions } from "react-router-dom";
import { useResourceNavigation, useResourceNavigationGuard } from "@/contexts/resource-navigation-context";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { useDocumentHistoryGuard } from "@/hooks/use-document-history-guard";
import { Settings, X } from "lucide-react";
import { SETTINGS_SECTIONS as SECTIONS, settingsSection } from "@/lib/settings-sections";
import { getAuthConfig, getMe, listPATs, adminListUsers, type AuthConfig, type AdminUser } from "@/lib/api";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { SelectMenu } from "@/components/ui/select-menu";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ProfileSection, type ProfileUserPatch, type User } from "./profile-section";
import { TokensSection, type PAT } from "./tokens-section";
import { PreferencesSection } from "./preferences-section";
import { AdminSection } from "./admin-section";
import { NotificationsSection } from "./notifications-section";
import { SecuritySection } from "./security-section";

type TabId = (typeof SECTIONS)[number]["id"];
type Pending = { type: "close" } | { type: "tab"; tab: TabId } | { type: "navigation"; href: string; options?: NavigateOptions } | { type: "history"; proceed: () => void };

export default function SettingsDialog({ initialTab = "profile", onClose, onNavigate, onRestoreFocus }: {
  initialTab?: string; onClose: () => void; onNavigate?: () => void; onRestoreFocus?: () => void;
}) {
  const navigate = useNavigate();
  const { completeSessionNavigation } = useResourceNavigation();
  const { checking, revision } = useAccessVerification();
  const shellUser = useCurrentUser();
  const readGeneration = useRef(0);
  const patsGeneration = useRef(0);
  const usersGeneration = useRef(0);
  const [patsLoading, setPatsLoading] = useState(false);
  const [usersLoading, setUsersLoading] = useState(false);
  const [loadedRevision, setLoadedRevision] = useState<number | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [accountError, setAccountError] = useState(false);
  const [pats, setPats] = useState<PAT[] | null>(null);
  const [patsError, setPatsError] = useState(false);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [usersError, setUsersError] = useState(false);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [requestedTab, setRequestedTab] = useState(initialTab);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const confirmFocus = useRef<HTMLElement | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const mounted = useRef(true);
  const operationMounted = useRef(true);
  const latestAccount = useRef<string | null>(null);
  latestAccount.current = shellUser?.user_id ?? user?.user_id ?? null;
  useEffect(() => { operationMounted.current = true; return () => { operationMounted.current = false; }; }, []);
  const operationOwner = user?.user_id;
  const ownsOperation = useCallback(() => operationMounted.current && !!operationOwner && latestAccount.current === operationOwner, [operationOwner]);
  const sections = SECTIONS.filter(section => section.id !== "admin" || user?.is_admin);
  const activeTab = settingsSection(requestedTab, !!user?.is_admin).id;
  const loadAccount = useCallback(async () => {
    const generation = ++readGeneration.current;
    setAccountError(false);
    try {
      const [account, config] = await Promise.all([getMe(), getAuthConfig()]);
      if (mounted.current && generation === readGeneration.current) { setUser(account); setAuthConfig(config); setLoadedRevision(revision); }
    } catch { if (mounted.current && generation === readGeneration.current) setAccountError(true); }
  }, [revision]);
  useEffect(() => {
    mounted.current = true;
    if (!checking) void loadAccount();
    return () => { mounted.current = false; readGeneration.current += 1; };
  }, [loadAccount, checking]);
  const verified = !checking && loadedRevision === revision && !accountError;
  const verifiedRef = useRef(verified);
  verifiedRef.current = verified;
  const reportDirty = useCallback((value: boolean) => { if (value || verifiedRef.current) setDirty(value); }, []);
  const reportBusy = useCallback((value: boolean) => { if (value || verifiedRef.current) setBusy(value); }, []);
  useEffect(() => {
    setPats(null); setUsers(null); setPatsError(false); setUsersError(false);
  }, [checking, revision]);
  useEffect(() => { content.current?.scrollTo?.({ top: 0 }); }, [activeTab]);
  useEffect(() => {
    if (!dirty && !busy) return;
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty, busy]);
  const loadPATs = useCallback(async () => {
    const generation = readGeneration.current;
    const request = ++patsGeneration.current;
    const current = () => mounted.current && generation === readGeneration.current && request === patsGeneration.current;
    setPatsLoading(true); setPatsError(false);
    try { const data = await listPATs(); if (current()) setPats(data.tokens || []); return current(); }
    catch (error) { if (current()) { setPatsError(true); if ([401, 403].includes((error as { status?: number })?.status ?? 0)) setPats(null); } return false; }
    finally { if (current()) setPatsLoading(false); }
  }, []);
  const loadUsers = useCallback(async () => {
    const generation = readGeneration.current;
    const request = ++usersGeneration.current;
    const current = () => mounted.current && generation === readGeneration.current && request === usersGeneration.current;
    setUsersLoading(true); setUsersError(false);
    try { const data = await adminListUsers(); if (current()) setUsers(data.users || []); return current(); }
    catch (error) { if (current()) { setUsersError(true); if ([401, 403].includes((error as { status?: number })?.status ?? 0)) setUsers(null); } return false; }
    finally { if (current()) setUsersLoading(false); }
  }, []);
  const userId = user?.user_id;
  useEffect(() => { if (verified && userId && activeTab === "tokens") void loadPATs(); }, [verified, revision, userId, activeTab, loadPATs]);
  useEffect(() => { if (verified && user?.is_admin && activeTab === "admin") void loadUsers(); }, [verified, revision, user?.is_admin, activeTab, loadUsers]);

  function execute(action: Pending) {
    if (action.type === "close") onClose();
    else if (action.type === "tab") { setDirty(false); setRequestedTab(action.tab); }
    else if (action.type === "history") {
      // Start the indexed replay while its owner is still mounted, then let
      // a lower editor's guard display its own confirmation unobscured.
      action.proceed();
      onNavigate?.();
    }
    else {
      continueNavigation(() => navigate(action.href, action.options));
      // A lower workspace guard may own the next confirmation. Remove only
      // this overlay, without the legacy /settings redirect.
      onNavigate?.();
    }
  }
  function request(action: Pending) {
    if (busy || pending || (action.type === "tab" && action.tab === activeTab)) return;
    if (dirty) {
      confirmFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setPending(action);
    } else execute(action);
  }
  const continueNavigation = useResourceNavigationGuard(dirty || busy ? (href, options) => {
    request({ type: "navigation", href, options });
    return false;
  } : null);
  useDocumentHistoryGuard(dirty || busy, proceed => request({ type: "history", proceed }));
  const localPasswordEnabled = authConfig?.available === true &&
    (authConfig.auth_mode === "local" || authConfig.auth_mode === "hybrid") && authConfig.local_auth.enabled;
  function updateUser(patch: ProfileUserPatch) {
    setUser(current => current ? { ...current, ...patch } : current);
    // Refresh the shared identity without remounting the underlying workspace.
    window.dispatchEvent(new Event("akb:revalidate-access"));
  }
  const navItem = ({ id, label, icon: Icon }: (typeof SECTIONS)[number]) => <TabsTrigger key={id} value={id} disabled={busy}
    className="min-h-10 w-full justify-start gap-2.5 whitespace-normal rounded-[var(--radius-sm)] px-3 py-2 text-left text-sm font-normal leading-5 hover:bg-surface-hover data-[state=active]:bg-surface-selected data-[state=active]:font-medium data-[state=active]:text-surface-selected-foreground data-[state=active]:shadow-none">
    <Icon className="h-4 w-4 shrink-0" aria-hidden /><span>{label}</span>
  </TabsTrigger>;

  // Own actual close gestures explicitly. A native photo picker can move
  // focus outside Radix before its selected-file/dirty state is observable.
  return <Dialog open>
    <DialogContent hideClose data-testid="account-settings-dialog"
      className="left-0 top-0 flex h-dvh max-h-dvh w-screen max-w-none translate-x-0 translate-y-0 flex-col gap-0 overflow-hidden rounded-none p-0 sm:left-1/2 sm:top-1/2 sm:h-[min(47.5rem,calc(100dvh-3rem))] sm:max-h-[calc(100dvh-3rem)] sm:w-[calc(100%-3rem)] sm:max-w-[65rem] sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-[var(--radius-xl)]"
      onOpenAutoFocus={event => { event.preventDefault(); heading.current?.focus(); }}
      onEscapeKeyDown={event => { event.preventDefault(); request({ type: "close" }); }}
      onPointerDownOutside={event => { event.preventDefault(); request({ type: "close" }); }}
      onInteractOutside={event => event.preventDefault()}
      onCloseAutoFocus={event => { event.preventDefault(); onRestoreFocus?.(); }}>
      <header className="flex min-h-14 shrink-0 items-center justify-between gap-3 border-b border-border px-4 sm:px-5">
        <div className="flex items-center gap-2.5"><Settings className="h-4 w-4 text-foreground-muted" aria-hidden /><DialogTitle ref={heading} tabIndex={-1} className="text-base outline-none">Settings</DialogTitle></div>
        <DialogDescription className="sr-only">Manage your account, appearance and agent connections without leaving your workspace.</DialogDescription>
        <Button variant="ghost" size="icon" disabled={busy} aria-label="Close settings" className="h-10 w-10 shrink-0 text-foreground-muted" onClick={() => request({ type: "close" })}><X className="h-4 w-4" aria-hidden /></Button>
      </header>
      <Tabs value={activeTab} onValueChange={tab => request({ type: "tab", tab: tab as TabId })} orientation="vertical" activationMode="manual" className="flex min-h-0 flex-1 flex-col md:flex-row">
        <aside aria-label="Settings navigation" className="shrink-0 border-b border-border md:flex md:w-52 md:flex-col md:border-b-0 md:border-r">
          <div className="p-3 md:hidden"><SelectMenu aria-label="Settings section" value={activeTab} disabled={busy} onValueChange={tab => request({ type: "tab", tab: tab as TabId })} options={sections.map(({ id, label }) => ({ value: id, label }))} className="min-h-11 w-full" /></div>
          <TabsList aria-label="Account settings" className="hidden min-h-0 flex-1 flex-col items-stretch justify-start gap-1 overflow-y-auto rounded-none bg-transparent p-3 md:flex rail-scroll">
            <span className="px-3 pb-2 pt-1 text-xs font-medium text-foreground-muted">Account</span>
            {sections.filter(section => !["tokens", "admin"].includes(section.id)).map(navItem)}
            <span className="px-3 pb-2 pt-5 text-xs font-medium text-foreground-muted">Connections</span>
            {sections.filter(section => section.id === "tokens").map(navItem)}
            {user?.is_admin && <><span className="px-3 pb-2 pt-5 text-xs font-medium text-foreground-muted">Workspace</span>{sections.filter(section => section.id === "admin").map(navItem)}</>}
          </TabsList>
        </aside>
        <div ref={content} data-slot="settings-content" className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain p-5 sm:p-7 rail-scroll">
          {busy && <p role="status" className="mb-4 text-sm text-foreground-muted">Saving changes… Keep settings open until this finishes.</p>}
          {accountError ? <Alert variant="destructive">Could not load your account settings. <Button variant="outline" size="sm" disabled={checking} onClick={() => void loadAccount()}>Retry</Button></Alert> : !verified ? <SettingsLoading /> : null}
          {/* Profile's operation owner stays active during proof; only its UI is
              suspended. Its callbacks therefore report actual completion, not
              the cleanup of a temporarily hidden Activity. */}
          {user && activeTab === "profile" && <TabsContent value="profile" className="pt-0">{verified && authConfig?.available === false && <Alert variant="warning" className="mb-4">Account editing is temporarily unavailable because authentication settings could not be verified. <Button variant="outline" size="sm" onClick={() => void loadAccount()}>Retry</Button></Alert>}<ProfileSection key={user.user_id} visible={verified} user={user} localPasswordEnabled={localPasswordEnabled} localProfileEditingEnabled={localPasswordEnabled} onDirtyChange={setDirty} onBusyChange={setBusy} onUserUpdate={updateUser} onPasswordChanged={() => completeSessionNavigation(() => navigate("/auth?reason=password-changed", { replace: true }))} /></TabsContent>}
          {user && activeTab === "security" && <TabsContent value="security" className="pt-0"><SecuritySection key={user.user_id} visible={verified} user={user} ownsOperation={ownsOperation} onBusyChange={setBusy} onDirtyChange={setDirty} /></TabsContent>}
          {user?.is_admin && activeTab === "admin" && <TabsContent value="admin" className="pt-0"><AdminSection key={user.user_id} visible={verified} ownsOperation={ownsOperation} user={user} onDirtyChange={setDirty} onBusyChange={setBusy} users={users} usersError={usersError} usersLoading={usersLoading} localPasswordEnabled={localPasswordEnabled} onReloadUsers={loadUsers} /></TabsContent>}
          <Activity mode={verified ? "visible" : "hidden"}><div>{user && <>
            <TabsContent value="preferences" className="pt-0"><PreferencesSection /></TabsContent>
            <TabsContent value="notifications" className="pt-0"><NotificationsSection onBusyChange={reportBusy} /></TabsContent>
            <TabsContent value="tokens" className="pt-0"><TokensSection key={user.user_id} visible={verified} ownsOperation={ownsOperation} pats={pats} patsError={patsError} patsLoading={patsLoading} mcpOauthEnabled={authConfig?.available === true && authConfig.mcp_oauth.enabled} onReloadPats={loadPATs} onDirtyChange={reportDirty} onBusyChange={reportBusy} /></TabsContent>
          </>}</div></Activity>
        </div>
      </Tabs>
      <ConfirmDialog open={verified && pending !== null} onOpenChange={open => { if (!open) setPending(null); }} returnFocusRef={confirmFocus}
        title={activeTab === "tokens" ? "Leave agent connections?" : "Discard unsaved changes?"}
        description={activeTab === "tokens" ? "Unfinished token setup will be cleared. If you created a token, copy and store its secret first; it cannot be retrieved again. Leaving does not revoke it." : "Your profile, photo or password changes have not been saved. Keep editing, or discard them to continue."}
        confirmLabel={activeTab === "tokens" ? "Leave section" : "Discard changes"} cancelLabel={activeTab === "tokens" ? "Stay here" : "Keep editing"} variant="destructive"
        onConfirm={() => { if (pending) { setDirty(false); execute(pending); } }} />
    </DialogContent>
  </Dialog>;
}

function SettingsLoading() {
  return <LoadingState label="Loading account settings" className="space-y-6"><Skeleton className="h-6 w-36" /><Skeleton className="h-16 w-full" /><div className="space-y-4">{[0, 1].map(item => <div key={item} className="space-y-2"><Skeleton className="h-3 w-24" /><Skeleton className="h-10 w-full" /></div>)}</div></LoadingState>;
}
