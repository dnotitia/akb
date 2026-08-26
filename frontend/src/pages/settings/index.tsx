import { useEffect, useState, type ComponentType } from "react";
import { useSearchParams } from "react-router-dom";
import {
  KeyRound,
  Palette,
  ShieldCheck,
  UserRound,
  type LucideProps,
} from "lucide-react";
import {
  getAuthConfig,
  getMe,
  listPATs,
  adminListUsers,
  type AuthConfig,
  type AdminUser,
} from "@/lib/api";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { PageHeader } from "@/components/ui/page-header";
import { PageShell } from "@/components/ui/page-shell";
import { RoleBadge } from "@/components/status-badge";
import { cn } from "@/lib/utils";
import { ProfileSection, type User } from "./profile-section";
import { TokensSection, type PAT } from "./tokens-section";
import { PreferencesSection } from "./preferences-section";
import { AdminSection } from "./admin-section";

type TabId = "profile" | "tokens" | "preferences" | "admin";

const SETTINGS_SECTIONS: Array<{
  id: Exclude<TabId, "admin">;
  label: string;
  description: string;
  icon: ComponentType<LucideProps>;
}> = [
  {
    id: "profile",
    label: "Profile",
    description: "Identity and password",
    icon: UserRound,
  },
  {
    id: "tokens",
    label: "Agent access",
    description: "Tokens and connections",
    icon: KeyRound,
  },
  {
    id: "preferences",
    label: "Appearance",
    description: "Theme preference",
    icon: Palette,
  },
];

export default function SettingsPage() {
  const [user, setUser] = useState<User | null>(null);
  const [pats, setPats] = useState<PAT[] | null>(null);
  const [patsError, setPatsError] = useState(false);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [usersError, setUsersError] = useState(false);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [verticalNav, setVerticalNav] = useState(() =>
    typeof window !== "undefined" ? window.matchMedia("(min-width: 1024px)").matches : false,
  );

  const [searchParams, setSearchParams] = useSearchParams();

  // Name the tab/history entry (tab switching + SR route-change orientation).
  // Keyed on the raw `?tab=` (the derived activeTab lives past an early return,
  // so it can't drive a hook).
  useEffect(() => {
    const tab = searchParams.get("tab") || "profile";
    const cap = tab.charAt(0).toUpperCase() + tab.slice(1);
    const prev = document.title;
    document.title = `Settings · ${cap} · AKB`;
    return () => {
      document.title = prev;
    };
  }, [searchParams]);

  useEffect(() => {
    let cancelled = false;
    void getMe()
      .then((u) => {
        if (!cancelled) setUser(u);
      })
      .catch(() => {
        location.href = "/auth";
      });
    void getAuthConfig().then((config) => {
      if (!cancelled) setAuthConfig(config);
    });
    void loadPATs();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 1024px)");
    const syncOrientation = () => setVerticalNav(media.matches);
    syncOrientation();
    media.addEventListener("change", syncOrientation);
    return () => media.removeEventListener("change", syncOrientation);
  }, []);

  async function loadPATs() {
    setPatsError(false);
    try {
      const d = await listPATs();
      setPats(d.tokens || []);
    } catch {
      // Leave pats null and flag — the Tokens tab shows a retry instead of a
      // deceptive "no tokens yet" empty state masking a fetch failure.
      setPatsError(true);
    }
  }

  async function loadUsers() {
    setUsersError(false);
    try {
      const d = await adminListUsers();
      setUsers(d.users || []);
    } catch {
      // Leave users null and flag — the Admin tab shows a retry instead of a
      // permanently-stuck "LOADING…".
      setUsersError(true);
    }
  }

  // Lazy-load the admin roster only when the Admin tab is actually viewed — an
  // admin landing on Profile/Tokens shouldn't pay the (potentially large)
  // /admin/users round-trip. Re-runs if a prior load errored and the user
  // returns to the tab.
  useEffect(() => {
    if (!user?.is_admin) return;
    if (searchParams.get("tab") !== "admin") return;
    // usersError intentionally omitted from deps: an auto-load that errors
    // must NOT immediately re-fire (storm); recovery is the manual Retry.
    if (users === null && !usersError) loadUsers();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, searchParams, users]);

  if (!user) return null;

  // Active tab synced to `?tab=` so Profile/Tokens/etc. are deep-linkable.
  // `admin` is only a valid value when the viewer is an admin — otherwise
  // it falls back to the default so non-admins can't land on a blank pane.
  const allowedTabs: TabId[] = ["profile", "tokens", "preferences"];
  if (user.is_admin) allowedTabs.push("admin");
  const rawTab = searchParams.get("tab");
  const activeTab: TabId =
    rawTab && allowedTabs.includes(rawTab as TabId)
      ? (rawTab as TabId)
      : "profile";
  const localPasswordEnabled =
    authConfig?.available === true &&
    (authConfig.auth_mode === "local" || authConfig.auth_mode === "hybrid") &&
    authConfig.local_auth.enabled;
  const mcpOauthEnabled =
    authConfig?.available === true && authConfig.mcp_oauth.enabled;

  const setTab = (v: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", v);
    setSearchParams(next, { replace: true });
  };

  return (
    <PageShell
      header={
        <PageHeader
          eyebrow="Personal workspace"
          title="Account settings"
          subtitle="Manage how you appear, how agents connect, and how AKB looks on this device."
          className="mb-7"
        />
      }
      contentWidth="full"
    >
      <Tabs
        value={activeTab}
        onValueChange={setTab}
        orientation={verticalNav ? "vertical" : "horizontal"}
        className="grid items-start gap-5 lg:grid-cols-[16.5rem_minmax(0,1fr)] xl:gap-8 xl:grid-cols-[18rem_minmax(0,1fr)]"
      >
        <aside className="min-w-0 lg:sticky lg:top-20 lg:flex lg:min-h-[calc(100vh-14rem)] lg:flex-col lg:rounded-[var(--radius-md)] lg:border lg:border-border lg:bg-surface lg:p-4 lg:shadow-xs xl:p-5">
          <div className="hidden border-b border-border pb-5 lg:block">
            <span className="coord-ink">Account</span>
            <div className="mt-3 flex items-center gap-3">
              <span
                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full border border-border-strong bg-surface-selected text-sm font-semibold text-surface-selected-foreground"
                aria-hidden
              >
                {initialsFor(user.display_name?.trim() || user.username)}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold text-foreground">
                  {user.display_name?.trim() || user.username}
                </p>
                <p className="mt-0.5 truncate text-xs text-foreground-muted">@{user.username}</p>
              </div>
              <RoleBadge role={user.is_admin ? "admin" : "user"} />
            </div>
            <p className="mt-3 truncate text-xs text-foreground-muted">{user.email}</p>
          </div>

          <TabsList
            aria-label="Account settings"
            className="flex w-full max-w-full items-stretch gap-1 overflow-x-auto rounded-[var(--radius-lg)] border border-border bg-surface p-2 shadow-sm [scrollbar-width:none] [&::-webkit-scrollbar]:hidden lg:mt-4 lg:flex-col lg:overflow-visible lg:rounded-none lg:border-0 lg:bg-transparent lg:p-0 lg:shadow-none"
          >
            {SETTINGS_SECTIONS.map((section) => (
              <SettingsTab
                key={section.id}
                {...section}
                count={section.id === "tokens" ? pats?.length : undefined}
              />
            ))}
            {user.is_admin && (
              <SettingsTab
                id="admin"
                label="Administration"
                description="Users and server access"
                icon={ShieldCheck}
                count={users?.length}
              />
            )}
          </TabsList>

          <div className="mt-5 hidden items-center gap-2 border-t border-border pt-4 text-xs text-foreground-muted lg:mt-auto lg:flex">
            <span className="h-2 w-2 shrink-0 rounded-full bg-success" aria-hidden />
            <span>Signed in as <span className="font-medium text-foreground">@{user.username}</span></span>
          </div>
        </aside>

        <div className="min-w-0">
          <TabsContent value="profile" className="space-y-6 pt-0">
            <ProfileSection
              user={user}
              localPasswordEnabled={localPasswordEnabled}
              localProfileEditingEnabled={localPasswordEnabled}
              onUserUpdate={(patch) =>
                setUser((u) => (u ? { ...u, ...patch } : u))
              }
            />
          </TabsContent>

          <TabsContent value="tokens" className="space-y-6 pt-0">
            <TokensSection
              pats={pats}
              patsError={patsError}
              mcpOauthEnabled={mcpOauthEnabled}
              onReloadPats={loadPATs}
            />
          </TabsContent>

          <TabsContent value="preferences" className="space-y-6 pt-0">
            <PreferencesSection />
          </TabsContent>

          {user.is_admin && (
            <TabsContent value="admin" className="space-y-6 pt-0">
              <AdminSection
                user={user}
                users={users}
                usersError={usersError}
                localPasswordEnabled={localPasswordEnabled}
                onReloadUsers={loadUsers}
              />
            </TabsContent>
          )}
        </div>
      </Tabs>
    </PageShell>
  );
}

function SettingsTab({
  id,
  label,
  description,
  icon: Icon,
  count,
}: {
  id: TabId;
  label: string;
  description: string;
  icon: ComponentType<LucideProps>;
  count?: number;
}) {
  return (
    <TabsTrigger
      value={id}
      className={cn(
        "group relative min-h-11 min-w-max justify-start gap-2.5 px-3 py-2 text-left",
        "data-[state=active]:bg-surface-selected data-[state=active]:text-surface-selected-foreground data-[state=active]:shadow-none",
        "lg:w-full lg:min-w-0 lg:rounded-[var(--radius-md)] lg:pl-4 lg:py-2.5",
      )}
    >
      <span
        className="absolute inset-y-2 left-0 hidden w-0.5 rounded-full bg-primary opacity-0 transition-opacity group-data-[state=active]:opacity-100 lg:block"
        aria-hidden
      />
      <span className="hidden h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-sm)] border border-transparent bg-surface-2 text-foreground-muted transition-token group-data-[state=active]:border-primary/15 group-data-[state=active]:bg-surface group-data-[state=active]:text-primary lg:flex">
        <Icon className="h-4 w-4" aria-hidden />
      </span>
      <span className="min-w-0">
        <span className="block text-sm font-semibold">{label}</span>
        <span className="mt-0.5 hidden truncate text-[11px] font-normal text-foreground-muted lg:block">
          {description}
        </span>
      </span>
      {count !== undefined && (
        <span className="ml-auto hidden min-w-6 rounded-full bg-surface px-1.5 py-0.5 text-center text-[10px] font-semibold tabular-nums text-foreground-muted ring-1 ring-border lg:inline-block">
          {count}
        </span>
      )}
    </TabsTrigger>
  );
}

function initialsFor(label: string): string {
  const words = label.trim().split(/\s+/).filter(Boolean);
  if (words.length > 1) {
    return `${Array.from(words[0])[0] ?? ""}${Array.from(words[words.length - 1])[0] ?? ""}`;
  }
  return Array.from(words[0] ?? "?").slice(0, 2).join("");
}
