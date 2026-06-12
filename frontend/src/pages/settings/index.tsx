import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import {
  getMe,
  listPATs,
  getToken,
  adminListUsers,
  type AdminUser,
} from "@/lib/api";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { ProfileSection, type User } from "./profile-section";
import { TokensSection, type PAT } from "./tokens-section";
import { PreferencesSection } from "./preferences-section";
import { AdminSection } from "./admin-section";

type TabId = "profile" | "tokens" | "preferences" | "admin";

export default function SettingsPage() {
  const [user, setUser] = useState<User | null>(null);
  const [pats, setPats] = useState<PAT[] | null>(null);
  const [patsError, setPatsError] = useState(false);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [usersError, setUsersError] = useState(false);

  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();

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

  // Walk back if there's history (user entered Settings from somewhere
  // meaningful — a vault, a doc, etc.), otherwise fall through to Home.
  // Browser history length hits 1 on a fresh tab / direct deep-link.
  const goBack = () => {
    if (window.history.length > 1) navigate(-1);
    else navigate("/");
  };

  useEffect(() => {
    if (!getToken()) {
      location.href = "/auth";
      return;
    }
    getMe()
      .then((u) => {
        setUser(u);
      })
      .catch(() => {
        location.href = "/auth";
      });
    loadPATs();
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

  const setTab = (v: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", v);
    setSearchParams(next, { replace: true });
  };

  return (
    <div className="max-w-4xl mx-auto fade-up">
      {/* One upward affordance — a history-aware Back. The breadcrumb's
          location (Settings › {tab}) was redundant with the H1 + the tab bar
          right below, and its middle crumb self-linked to this page. */}
      <div className="mb-6">
        <button
          type="button"
          onClick={goBack}
          className="inline-flex items-center gap-1.5 min-h-[36px] coord hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background rounded-[var(--radius-sm)]"
        >
          <ArrowLeft className="h-3 w-3" aria-hidden />
          Back
        </button>
      </div>

      <header className="mb-6">
        <div className="coord-spark mb-2">Account · {user.username}</div>
        <h1 className="font-display text-3xl text-foreground">
          Settings
        </h1>
        <p className="mt-1.5 text-sm text-foreground-muted">
          Manage your account, connection tokens, and preferences.
        </p>
      </header>

      <Tabs value={activeTab} onValueChange={setTab}>
        {/* Scroll the pill track on narrow screens so the admin 4-tab row never
            clips at 375px (the raised pills break if wrapped). */}
        <TabsList className="max-w-full overflow-x-auto">
          <TabsTrigger value="profile">Profile</TabsTrigger>
          <TabsTrigger value="tokens" className="gap-1.5">
            Tokens
            <span className="coord tabular-nums">[{pats ? pats.length : "··"}]</span>
          </TabsTrigger>
          <TabsTrigger value="preferences">Preferences</TabsTrigger>
          {user.is_admin && (
            <TabsTrigger value="admin" className="gap-1.5">
              Admin
              {users && (
                <span className="coord tabular-nums">[{users.length}]</span>
              )}
            </TabsTrigger>
          )}
        </TabsList>

        <TabsContent value="profile" className="pt-6 space-y-6">
          <ProfileSection
            user={user}
            onUserUpdate={(patch) =>
              setUser((u) => (u ? { ...u, ...patch } : u))
            }
          />
        </TabsContent>

        <TabsContent value="tokens" className="pt-6 space-y-6">
          <TokensSection
            pats={pats}
            patsError={patsError}
            onReloadPats={loadPATs}
          />
        </TabsContent>

        {/* Preferences — theme control inline (synced with the header menu via
            useTheme), not a read-only status pointing off-page. */}
        <TabsContent value="preferences" className="pt-6 space-y-6">
          <PreferencesSection />
        </TabsContent>

        {/* The "Memory" tab was removed in v0.5.0 — agent memory now
            lives in a per-user vault (`agent-memory-{username}`) and is
            accessible via the standard /vault/ browse UI. */}

        {/* Admin — user management. Only rendered when user.is_admin. */}
        {user.is_admin && (
          <TabsContent value="admin" className="pt-6 space-y-6">
            <AdminSection
              user={user}
              users={users}
              usersError={usersError}
              onReloadUsers={loadUsers}
            />
          </TabsContent>
        )}
      </Tabs>
    </div>
  );
}
