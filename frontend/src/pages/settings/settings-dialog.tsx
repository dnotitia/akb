import { useCallback, useEffect, useRef, useState } from "react";
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
import { ProfileSection, type User } from "./profile-section";
import { TokensSection, type PAT } from "./tokens-section";
import { PreferencesSection } from "./preferences-section";
import { AdminSection } from "./admin-section";
import { NotificationsSection } from "./notifications-section";
import { SecuritySection } from "./security-section";

type TabId = (typeof SECTIONS)[number]["id"];
type Pending = { type: "close" } | { type: "tab"; tab: TabId };

export default function SettingsDialog({ initialTab = "profile", onClose, onRestoreFocus }: {
  initialTab?: string; onClose: () => void; onRestoreFocus?: () => void;
}) {
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
  const sections = SECTIONS.filter(section => section.id !== "admin" || user?.is_admin);
  const activeTab = settingsSection(requestedTab, !!user?.is_admin).id;
  const loadAccount = useCallback(async () => {
    setAccountError(false);
    try {
      const [account, config] = await Promise.all([getMe(), getAuthConfig()]);
      if (mounted.current) { setUser(account); setAuthConfig(config); }
    } catch { if (mounted.current) setAccountError(true); }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void loadAccount();
    return () => { mounted.current = false; };
  }, [loadAccount]);
  useEffect(() => { content.current?.scrollTo?.({ top: 0 }); }, [activeTab]);
  useEffect(() => {
    if (!dirty && !busy) return;
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty, busy]);
  const loadPATs = useCallback(async () => {
    setPatsError(false);
    try { const data = await listPATs(); if (mounted.current) setPats(data.tokens || []); }
    catch { if (mounted.current) setPatsError(true); }
  }, []);
  const loadUsers = useCallback(async () => {
    setUsersError(false);
    try { const data = await adminListUsers(); if (mounted.current) setUsers(data.users || []); }
    catch { if (mounted.current) setUsersError(true); }
  }, []);
  const userId = user?.user_id;
  useEffect(() => { if (userId && activeTab === "tokens") void loadPATs(); }, [userId, activeTab, loadPATs]);
  useEffect(() => { if (user?.is_admin && activeTab === "admin") void loadUsers(); }, [user?.is_admin, activeTab, loadUsers]);

  function execute(action: Pending) {
    if (action.type === "close") onClose();
    else { setDirty(false); setRequestedTab(action.tab); }
  }
  function request(action: Pending) {
    if (busy || pending || (action.type === "tab" && action.tab === activeTab)) return;
    if (dirty) {
      confirmFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setPending(action);
    } else execute(action);
  }
  const localPasswordEnabled = authConfig?.available === true &&
    (authConfig.auth_mode === "local" || authConfig.auth_mode === "hybrid") && authConfig.local_auth.enabled;
  function updateUser(patch: { display_name?: string; email?: string }) {
    setUser(current => current ? { ...current, ...patch } : current);
    // Refresh the shared identity without remounting the underlying workspace.
    window.dispatchEvent(new Event("akb:revalidate-access"));
  }
  const navItem = ({ id, label, icon: Icon }: (typeof SECTIONS)[number]) => <TabsTrigger key={id} value={id} disabled={busy}
    className="min-h-10 w-full justify-start gap-2.5 whitespace-normal rounded-[var(--radius-sm)] px-3 py-2 text-left text-sm font-normal leading-5 hover:bg-surface-hover data-[state=active]:bg-surface-selected data-[state=active]:font-medium data-[state=active]:text-surface-selected-foreground data-[state=active]:shadow-none">
    <Icon className="h-4 w-4 shrink-0" aria-hidden /><span>{label}</span>
  </TabsTrigger>;

  return <Dialog open onOpenChange={open => { if (!open) request({ type: "close" }); }}>
    <DialogContent hideClose data-testid="account-settings-dialog"
      className="left-0 top-0 flex h-dvh max-h-dvh w-screen max-w-none translate-x-0 translate-y-0 flex-col gap-0 overflow-hidden rounded-none p-0 sm:left-1/2 sm:top-1/2 sm:h-[min(47.5rem,calc(100dvh-3rem))] sm:max-h-[calc(100dvh-3rem)] sm:w-[calc(100%-3rem)] sm:max-w-[65rem] sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-[var(--radius-xl)]"
      onOpenAutoFocus={event => { event.preventDefault(); heading.current?.focus(); }}
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
          {accountError ? <Alert variant="destructive">Could not load your account settings. <Button variant="outline" size="sm" onClick={() => void loadAccount()}>Retry</Button></Alert> : !user ? <SettingsLoading /> : <>
            <TabsContent value="profile" className="pt-0">{authConfig?.available === false && <Alert variant="warning" className="mb-4">Account editing is temporarily unavailable because authentication settings could not be verified. <Button variant="outline" size="sm" onClick={() => void loadAccount()}>Retry</Button></Alert>}<ProfileSection user={user} localPasswordEnabled={localPasswordEnabled} localProfileEditingEnabled={localPasswordEnabled} onDirtyChange={setDirty} onBusyChange={setBusy} onUserUpdate={updateUser} /></TabsContent>
            <TabsContent value="security" className="pt-0"><SecuritySection key={user.user_id} user={user} onBusyChange={setBusy} /></TabsContent>
            <TabsContent value="preferences" className="pt-0"><PreferencesSection /></TabsContent>
            <TabsContent value="notifications" className="pt-0"><NotificationsSection onBusyChange={setBusy} /></TabsContent>
            <TabsContent value="tokens" className="pt-0"><TokensSection pats={pats} patsError={patsError} mcpOauthEnabled={authConfig?.available === true && authConfig.mcp_oauth.enabled} onReloadPats={loadPATs} onDirtyChange={setDirty} onBusyChange={setBusy} /></TabsContent>
            {user.is_admin && <TabsContent value="admin" className="pt-0"><AdminSection user={user} users={users} usersError={usersError} localPasswordEnabled={localPasswordEnabled} onReloadUsers={loadUsers} /></TabsContent>}
          </>}
        </div>
      </Tabs>
      <ConfirmDialog open={pending !== null} onOpenChange={open => { if (!open) setPending(null); }} returnFocusRef={confirmFocus}
        title={activeTab === "tokens" ? "Leave agent connections?" : "Discard unsaved changes?"}
        description={activeTab === "tokens" ? "Unfinished token setup will be cleared. If you created a token, copy and store its secret first; it cannot be retrieved again. Leaving does not revoke it." : "Your profile or password changes have not been saved. Keep editing, or discard them to continue."}
        confirmLabel={activeTab === "tokens" ? "Leave section" : "Discard changes"} cancelLabel={activeTab === "tokens" ? "Stay here" : "Keep editing"} variant="destructive"
        onConfirm={() => { if (pending) { setDirty(false); execute(pending); } }} />
    </DialogContent>
  </Dialog>;
}

function SettingsLoading() {
  return <LoadingState label="Loading account settings" className="space-y-6"><Skeleton className="h-6 w-36" /><Skeleton className="h-16 w-full" /><div className="space-y-4">{[0, 1].map(item => <div key={item} className="space-y-2"><Skeleton className="h-3 w-24" /><Skeleton className="h-10 w-full" /></div>)}</div></LoadingState>;
}
