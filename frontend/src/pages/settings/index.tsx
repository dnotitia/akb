import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Settings } from "lucide-react";
import { SETTINGS_SECTIONS as SECTIONS, settingsSection } from "@/lib/settings-sections";
import { getAuthConfig, getMe, listPATs, adminListUsers, type AuthConfig, type AdminUser } from "@/lib/api";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { SelectMenu } from "@/components/ui/select-menu";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ProfileSection, type User } from "./profile-section";
import { TokensSection, type PAT } from "./tokens-section";
import { PreferencesSection } from "./preferences-section";
import { AdminSection } from "./admin-section";
import { NotificationsSection } from "./notifications-section";

type TabId = "profile" | "tokens" | "preferences" | "notifications" | "admin";

export default function SettingsPage() {
  const [user, setUser] = useState<User | null>(null);
  const [pats, setPats] = useState<PAT[] | null>(null);
  const [patsError, setPatsError] = useState(false);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [usersError, setUsersError] = useState(false);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const [dirty, setDirty] = useState(false);
  const [navigationBusy, setNavigationBusy] = useState(false);
  const [pendingTab, setPendingTab] = useState<TabId | null>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const content = useRef<HTMLDivElement>(null);
  const sections = SECTIONS.filter(section => section.id !== "admin" || user?.is_admin);
  const activeTab = settingsSection(searchParams.get("tab"), !!user?.is_admin).id;

  useEffect(() => {
    const previous = document.title;
    document.title = `${SECTIONS.find(section => section.id === activeTab)?.label} · Settings · AKB`;
    content.current?.scrollTo?.({ top: 0 });
    return () => { document.title = previous; };
  }, [activeTab]);
  useEffect(() => {
    if (!dirty && !navigationBusy) return;
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty, navigationBusy]);
  useEffect(() => {
    let cancelled = false;
    void getMe().then(value => { if (!cancelled) setUser(value); }).catch(() => { location.href = "/auth"; });
    void getAuthConfig().then(value => { if (!cancelled) setAuthConfig(value); }).catch(() => { if (!cancelled) setAuthConfig(null); });
    return () => { cancelled = true; };
  }, []);
  async function loadPATs() {
    setPatsError(false);
    try { const data = await listPATs(); setPats(data.tokens || []); }
    catch { setPatsError(true); }
  }
  async function loadUsers() {
    setUsersError(false);
    try { const data = await adminListUsers(); setUsers(data.users || []); }
    catch { setUsersError(true); }
  }
  useEffect(() => {
    if (activeTab === "tokens" && pats === null && !patsError) void loadPATs();
    // Retry is explicit; a failed request must not trigger a request loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);
  useEffect(() => {
    if (user?.is_admin && activeTab === "admin" && users === null && !usersError) void loadUsers();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, activeTab]);
  function changeTab(tab: TabId) {
    const next = new URLSearchParams(searchParams);
    next.set("tab", tab);
    setSearchParams(next);
  }
  function requestTab(value: string) {
    if (navigationBusy) return;
    const tab = value as TabId;
    if (tab === activeTab) return;
    if (dirty) {
      returnFocus.current = document.activeElement as HTMLElement | null;
      setPendingTab(tab);
    } else changeTab(tab);
  }
  const localPasswordEnabled = authConfig?.available === true &&
    (authConfig.auth_mode === "local" || authConfig.auth_mode === "hybrid") && authConfig.local_auth.enabled;

  return <Tabs value={activeTab} onValueChange={requestTab} orientation="vertical" activationMode="manual"
    data-testid="settings-workspace" className="relative flex h-full min-h-0 flex-col bg-surface lg:-mt-14 lg:h-[calc(100%+3.5rem)] lg:flex-row">
    <h1 className="sr-only">Account settings</h1>
    <aside aria-label="Settings navigation" className="shrink-0 border-b border-border bg-surface lg:flex lg:w-[13.75rem] lg:flex-col lg:border-b-0 lg:border-r">
      <div className="hidden h-14 shrink-0 items-center gap-2 border-b border-border px-4 text-sm font-semibold lg:flex">
        <Settings className="h-4 w-4 text-link" aria-hidden />Settings
      </div>
      <div className="flex items-center gap-3 p-3 lg:hidden">
        <Settings className="h-4 w-4 shrink-0 text-link" aria-hidden />
        <SelectMenu value={activeTab} disabled={navigationBusy} aria-label="Settings section" onValueChange={requestTab}
          options={sections.map(section => ({ value: section.id, label: section.label }))} className="min-w-0" />
      </div>
      <TabsList aria-label="Account settings" className="hidden min-h-0 flex-1 flex-col items-stretch justify-start gap-1 overflow-y-auto rounded-none bg-transparent p-2 shadow-none lg:flex rail-scroll">
        {sections.map(({ id, label, icon: Icon }) => <TabsTrigger key={id} value={id} disabled={navigationBusy}
          className={`relative min-h-9 shrink-0 justify-start gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm font-normal hover:bg-surface-hover data-[state=active]:bg-surface-selected data-[state=active]:font-medium data-[state=active]:text-surface-selected-foreground data-[state=active]:shadow-none ${id === "admin" ? "mt-3 border-t border-border pt-3" : ""}`}>
          <Icon className="h-4 w-4 shrink-0" aria-hidden /><span>{label}</span>
        </TabsTrigger>)}
      </TabsList>
    </aside>
    <div className="flex min-h-0 min-w-0 flex-1 flex-col lg:pt-14">
      <div ref={content} data-slot="settings-content" className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6 xl:p-8 rail-scroll">
        {!user ? <SettingsLoading /> : <>
          <TabsContent value="profile" className="pt-0">
            <ProfileSection user={user} localPasswordEnabled={localPasswordEnabled} localProfileEditingEnabled={localPasswordEnabled}
              onDirtyChange={setDirty} onUserUpdate={patch => setUser(current => current ? { ...current, ...patch } : current)} />
          </TabsContent>
          <TabsContent value="preferences" className="pt-0"><PreferencesSection /></TabsContent>
          <TabsContent value="notifications" className="pt-0"><NotificationsSection /></TabsContent>
          <TabsContent value="tokens" className="pt-0">
            <TokensSection pats={pats} patsError={patsError} mcpOauthEnabled={authConfig?.available === true && authConfig.mcp_oauth.enabled} onReloadPats={loadPATs} onDirtyChange={setDirty} onBusyChange={setNavigationBusy} />
          </TabsContent>
          {user.is_admin && <TabsContent value="admin" className="pt-0"><AdminSection user={user} users={users} usersError={usersError} localPasswordEnabled={localPasswordEnabled} onReloadUsers={loadUsers} /></TabsContent>}
        </>}
      </div>
    </div>
    <ConfirmDialog open={pendingTab !== null} onOpenChange={open => { if (!open) setPendingTab(null); }} returnFocusRef={returnFocus}
      title={activeTab === "tokens" ? "Have you saved your token?" : "Discard unsaved changes?"} description={activeTab === "tokens" ? "New token secrets cannot be retrieved again. Copy and store them before leaving this section." : "Your profile or password changes have not been saved. Stay here to keep editing, or discard them to switch sections."}
      confirmLabel={activeTab === "tokens" ? "Leave section" : "Discard changes"} cancelLabel={activeTab === "tokens" ? "Keep token visible" : "Keep editing"} variant="destructive" onConfirm={() => {
        if (pendingTab) { setDirty(false); changeTab(pendingTab); }
      }} />
  </Tabs>;
}

function SettingsLoading() {
  return <LoadingState label="Loading account settings" className="max-w-3xl space-y-6">
    <Skeleton className="h-5 w-36" />
    <div className="space-y-6 border-t border-border pt-6">{[0, 1, 2].map(item => <div key={item} className="space-y-2"><Skeleton className="h-3 w-24" /><Skeleton className="h-10 w-full max-w-lg" /></div>)}</div>
  </LoadingState>;
}
