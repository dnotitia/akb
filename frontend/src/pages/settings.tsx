import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  ArrowLeft,
  ArrowUpRight,
  ChevronRight,
  Copy,
  Eye,
  EyeOff,
  Key,
  Loader2,
  Plus,
  RotateCw,
  Trash2,
  X,
} from "lucide-react";
import {
  getMe,
  createPAT,
  listPATs,
  revokePAT,
  getToken,
  adminListUsers,
  adminDeleteUser,
  changePassword,
  updateProfile,
  type AdminUser,
} from "@/lib/api";
import { useDebounce } from "@/hooks/use-debounce";
import { AdminResetPasswordDialog } from "@/components/admin-reset-password-dialog";
import { formatDate } from "@/lib/utils";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SelectMenu } from "@/components/ui/select-menu";
import { Badge } from "@/components/ui/badge";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/empty-state";
import { useTheme } from "@/hooks/use-theme";
import { useFlashStatus } from "@/hooks/use-flash-status";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import {
  mcpInstallSnippets,
  MCP_AGENT_FILES,
  MCP_AGENT_LABELS,
  type McpAgent,
} from "@/lib/mcp-snippets";

interface User {
  user_id: string;
  username: string;
  email: string;
  display_name?: string;
  is_admin?: boolean;
}

interface PAT {
  token_id: string;
  name: string;
  prefix: string;
  created_at?: string;
  last_used_at?: string;
}

type TabId = "profile" | "tokens" | "preferences" | "admin";
type ClientTab = McpAgent;
type AdminSort = "recent" | "oldest" | "username" | "vaults";

export default function SettingsPage() {
  const [user, setUser] = useState<User | null>(null);
  const [pats, setPats] = useState<PAT[] | null>(null);
  const [newName, setNewName] = useState("");
  const [newPat, setNewPat] = useState<string | null>(null);
  const [showPat, setShowPat] = useState<boolean>(true);
  const [copied, setCopied] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [mintError, setMintError] = useState<string | null>(null);
  // Reissue = revoke-then-mint. Routed through a ConfirmDialog (the old token
  // dies immediately) with a per-row pending guard so a double-click can't
  // fire two revoke/mint pairs, and an error channel for the dangerous
  // half-failure where revoke lands but the replacement mint rejects.
  const [pendingReissue, setPendingReissue] = useState<PAT | null>(null);
  const [reissuingId, setReissuingId] = useState<string | null>(null);
  const [reissueError, setReissueError] = useState<string | null>(null);
  const [patsError, setPatsError] = useState(false);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [usersError, setUsersError] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [adminQuery, setAdminQuery] = useState("");
  const [adminSort, setAdminSort] = useState<AdminSort>("recent");
  const debouncedQuery = useDebounce(adminQuery, 200);
  const [clientTab, setClientTab] = useState<ClientTab>("claude");
  const [pendingDeleteUser, setPendingDeleteUser] = useState<AdminUser | null>(null);
  const [pendingRevokePat, setPendingRevokePat] = useState<PAT | null>(null);
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null);
  const [setupOpen, setSetupOpen] = useState<boolean | null>(() => {
    const saved = localStorage.getItem("akb:tokens-setup-open");
    if (saved === "true") return true;
    if (saved === "false") return false;
    return null;
  });
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwConfirm, setPwConfirm] = useState("");
  const [pwError, setPwError] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwTouched, setPwTouched] = useState({ new: false, confirm: false });
  const pwTooShort = pwTouched.new && pwNew.length > 0 && pwNew.length < 8;
  const pwMismatch =
    pwTouched.confirm && pwConfirm.length > 0 && pwNew !== pwConfirm;
  const pwSubmitDisabled =
    pwBusy ||
    pwNew.length < 8 ||
    pwNew !== pwConfirm ||
    pwCurrent.length === 0;
  const [profileDisplayName, setProfileDisplayName] = useState("");
  const [profileEmail, setProfileEmail] = useState("");
  const [profileError, setProfileError] = useState("");
  // Benign "nothing to save" message — kept off the red error channel so a
  // no-op submit doesn't read as a failure.
  const [profileNotice, setProfileNotice] = useState("");
  const [profileBusy, setProfileBusy] = useState(false);
  const profileFlash = useFlashStatus(3000);
  const passwordFlash = useFlashStatus(3000);
  const { theme } = useTheme();

  // Sync local edit state when user payload arrives.
  useEffect(() => {
    if (user) {
      setProfileDisplayName(user.display_name ?? "");
      setProfileEmail(user.email ?? "");
    }
  }, [user]);

  // Smart default: open setup guide when user has no PATs, closed otherwise.
  // Only applies when localStorage has no saved preference (setupOpen === null).
  useEffect(() => {
    if (setupOpen !== null) return;
    if (pats === null) return;
    setSetupOpen(pats.length === 0);
  }, [pats, setupOpen]);

  function toggleSetup() {
    const next = !setupOpen;
    setSetupOpen(next);
    localStorage.setItem("akb:tokens-setup-open", String(next));
  }

  async function handleSaveProfile(e: React.FormEvent) {
    e.preventDefault();
    if (!user) return;
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
      setUser({ ...user, display_name: res.display_name ?? undefined, email: res.email });
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
    } catch (e: any) {
      setPwError(e?.message || "Failed to change password");
    } finally {
      setPwBusy(false);
    }
  }

  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();

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

  async function confirmDeleteUser() {
    if (!pendingDeleteUser) return;
    const u = pendingDeleteUser;
    setDeletingId(u.id);
    try {
      await adminDeleteUser(u.id);
      await loadUsers();
    } finally {
      setDeletingId(null);
    }
  }

  async function confirmRevokePat() {
    if (!pendingRevokePat) return;
    await revokePAT(pendingRevokePat.token_id);
    await loadPATs();
  }

  async function copy(text: string, label: string) {
    // clipboard is undefined on insecure (plain-HTTP) origins — and AKB ships
    // an `--insecure` snippet, so that deploy shape is real. Guard with `?.` so
    // copying a show-once secret never throws an uncaught TypeError with no
    // feedback; the value stays on screen to copy manually.
    try {
      await navigator.clipboard?.writeText(text);
      setCopied(label);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      /* clipboard blocked — value remains visible for manual copy */
    }
  }

  async function handleCreatePAT(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    setMintError(null);
    setCreating(true);
    try {
      const r = await createPAT(newName);
      setNewPat(r.token);
      setShowPat(true);
      setNewName("");
      await loadPATs();
    } catch (err) {
      // No app-wide toast — surface inline or the button settles with no token
      // and no explanation on a secret the user is waiting for.
      setMintError(
        err instanceof Error ? err.message : "Couldn't mint a token. Please try again.",
      );
    } finally {
      setCreating(false);
    }
  }

  // Reissue = revoke the live token, then mint a replacement. Confirmed first
  // (the old token stops working the instant revoke lands). If the mint half
  // rejects after revoke succeeded, the deployed token is already gone — we
  // surface that explicitly instead of swallowing it.
  async function handleReissue(p: PAT) {
    setReissuingId(p.token_id);
    setReissueError(null);
    try {
      await revokePAT(p.token_id);
      const r = await createPAT(p.name);
      setNewPat(r.token);
      setShowPat(true);
      await loadPATs();
    } catch {
      setReissueError(
        `"${p.name}" was revoked but a replacement could not be minted — mint a new token now to restore access.`,
      );
      await loadPATs();
    } finally {
      setReissuingId(null);
    }
  }

  // Pat used in snippets: prefer fresh mint, else first active, else placeholder.
  const snippetPat = newPat || (pats?.[0] ? pats[0].prefix + "…" : "<YOUR_PAT>");
  const snippets = useMemo(() => mcpInstallSnippets(snippetPat), [snippetPat]);
  // Fresh-token banner embeds the real, un-masked token in its config block.
  const freshSnippet = useMemo(
    () => (newPat ? mcpInstallSnippets(newPat).cursor : ""),
    [newPat],
  );

  const filteredUsers = useMemo(() => {
    if (!users) return [];
    const q = debouncedQuery.trim().toLowerCase();
    let result = users;
    if (q) {
      result = users.filter(
        (u) =>
          u.username.toLowerCase().includes(q) ||
          u.email?.toLowerCase().includes(q) ||
          u.display_name?.toLowerCase().includes(q),
      );
    }
    const sorted = [...result];
    switch (adminSort) {
      case "recent":
        sorted.sort((a, b) => b.created_at.localeCompare(a.created_at));
        break;
      case "oldest":
        sorted.sort((a, b) => a.created_at.localeCompare(b.created_at));
        break;
      case "username":
        sorted.sort((a, b) => a.username.localeCompare(b.username));
        break;
      case "vaults":
        sorted.sort((a, b) => (b.owned_vaults || 0) - (a.owned_vaults || 0));
        break;
    }
    return sorted;
  }, [users, debouncedQuery, adminSort]);

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
    <div className="max-w-[1280px] mx-auto fade-up">
      <div className="flex items-center justify-between mb-6">
        <button
          type="button"
          onClick={goBack}
          className="inline-flex items-center gap-1.5 coord hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <ArrowLeft className="h-3 w-3" aria-hidden />
          Back
        </button>
        <nav aria-label="Breadcrumb" className="flex items-center gap-2 coord">
          <Link to="/" className="hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background">Home</Link>
          <ChevronRight className="h-3 w-3 text-foreground-muted" aria-hidden />
          <Link to="/settings" className="hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background">Settings</Link>
          <ChevronRight className="h-3 w-3 text-foreground-muted" aria-hidden />
          <span className="text-foreground capitalize">{activeTab}</span>
        </nav>
      </div>

      <header className="mb-6">
        <div className="coord-spark mb-2">Settings</div>
        <h1 className="font-display text-3xl text-foreground">
          Settings
        </h1>
        <p className="mt-1.5 text-sm text-foreground-muted">
          Manage your account, connection tokens, and preferences.
        </p>
      </header>

      <Tabs value={activeTab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="profile">Profile</TabsTrigger>
          <TabsTrigger value="tokens" className="gap-1.5">
            Tokens
            <span className="coord tabular-nums">[{pats?.length ?? 0}]</span>
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

        {/* Profile — read-only account info */}
        <TabsContent value="profile" className="pt-6 max-w-4xl space-y-6">
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
                {user.is_admin ? (
                  <Badge variant="owner">ADMIN</Badge>
                ) : (
                  <div className="text-sm font-medium text-foreground">User</div>
                )}
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
              <Button type="submit" loading={profileBusy}>
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
                  <p id="pw-new-help" className="text-destructive text-xs font-mono mt-1">
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
                  <p id="pw-confirm-help" className="text-destructive text-xs font-mono mt-1">
                    Doesn&apos;t match new password.
                  </p>
                )}
              </div>
              {pwError && (
                <p role="alert" className="text-destructive text-xs font-mono">
                  {pwError}
                </p>
              )}
              {passwordFlash.message && (
                <p role="status" aria-live="polite" className="text-success text-xs font-mono">
                  {passwordFlash.message}
                </p>
              )}
              <Button type="submit" loading={pwBusy} disabled={pwSubmitDisabled} aria-disabled={pwSubmitDisabled}>
                Change password
              </Button>
            </form>
          </section>
        </TabsContent>

        {/* Tokens — PATs + fresh token banner when minted */}
        <TabsContent value="tokens" className="pt-6 space-y-4 max-w-4xl">
          {newPat && (
            <section
              className="rounded-[var(--radius-lg)] border border-accent/40 bg-accent/5 shadow-sm overflow-hidden"
              role="status"
              aria-live="polite"
            >
              <div className="border-b border-accent/40 px-4 py-2 flex items-baseline justify-between gap-2 flex-wrap">
                <div>
                  <span className="coord-spark">Fresh token — copy now</span>
                  <span className="coord ml-2">Shown once. If you dismiss without copying, you'll need to reissue.</span>
                </div>
                <button
                  onClick={() => setNewPat(null)}
                  aria-label="Dismiss fresh token"
                  className="inline-flex items-center justify-center h-7 w-7 coord hover:text-primary cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              </div>
              <div className="p-6 space-y-4">
                <div className="flex items-start gap-3">
                  <code className="flex-1 font-mono text-xs text-foreground break-all rounded-[var(--radius-md)] border border-border px-3 py-2 bg-surface">
                    {showPat ? newPat : newPat.slice(0, 12) + "•".repeat(20)}
                  </code>
                  {/* Full token stays reachable to a screen reader even masked. */}
                  {!showPat && <span className="sr-only">Token value: {newPat}</span>}
                  <button
                    onClick={() => setShowPat(!showPat)}
                    aria-label={showPat ? "Hide token" : "Show token"}
                    className="inline-flex items-center justify-center h-7 px-2 coord hover:text-primary cursor-pointer shrink-0 rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                  >
                    {showPat ? (
                      <EyeOff className="h-3 w-3" aria-hidden />
                    ) : (
                      <Eye className="h-3 w-3" aria-hidden />
                    )}
                  </button>
                  <button
                    onClick={() => copy(newPat, "pat")}
                    aria-label={copied === "pat" ? "Token copied" : "Copy token"}
                    className="inline-flex items-center justify-center h-7 px-2 coord hover:text-primary cursor-pointer shrink-0 rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                  >
                    {copied === "pat" ? <span aria-hidden>Copied</span> : <Copy className="h-3 w-3" aria-hidden />}
                  </button>
                </div>

                <CodeSnippet code={freshSnippet} filename={MCP_AGENT_FILES.cursor} />
              </div>
            </section>
          )}

          {/* Active tokens — primary content on this tab (management). */}
          <section className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden">
            <header className="border-b border-border px-6 py-3 flex items-baseline gap-3">
              <span className="coord-ink">Active tokens</span>
              <span className="coord tabular-nums">[{pats?.length ?? 0}]</span>
            </header>
            <div className="p-6 space-y-4">
              {reissueError && <Alert variant="destructive">{reissueError}</Alert>}
              {patsError ? (
                <EmptyState
                  title="Couldn't load tokens"
                  description="Something went wrong fetching your tokens."
                  action={
                    <Button variant="outline" size="sm" onClick={() => loadPATs()}>
                      Retry
                    </Button>
                  }
                />
              ) : !pats ? (
                <div className="coord" role="status" aria-live="polite">Loading</div>
              ) : pats.length === 0 ? (
                <EmptyState title="No tokens yet — mint one below." />
              ) : (
                <div className="rounded-[var(--radius-md)] border border-border divide-y divide-border overflow-hidden">
                  {(pats ?? []).map((p, i) => (
                    <div key={p.token_id} className="px-4 py-3 space-y-1.5">
                      {/* Line 1 — identity */}
                      <div className="flex items-baseline gap-3 min-w-0">
                        <span className="coord tabular-nums shrink-0">
                          {String(i + 1).padStart(2, "0")}
                        </span>
                        <span title={p.name} className="text-sm font-medium truncate text-foreground">
                          {p.name}
                        </span>
                        <code className="font-mono text-[11px] text-foreground-muted">
                          {p.prefix}••••
                        </code>
                      </div>
                      {/* Line 2 — meta + actions */}
                      <div className="flex items-center justify-between gap-3 flex-wrap pl-7">
                        <div className="flex items-center gap-3 text-foreground-muted">
                          <span className="coord tabular-nums">
                            Created {formatDate(p.created_at)}
                          </span>
                          {p.last_used_at && (
                            <span className="coord tabular-nums">
                              Used {formatDate(p.last_used_at)}
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-1 ml-auto">
                          <button
                            onClick={() => setPendingReissue(p)}
                            disabled={reissuingId === p.token_id}
                            aria-label={`Reissue token ${p.name}`}
                            className="inline-flex items-center gap-1 px-2 h-7 rounded-[var(--radius-sm)] text-xs text-foreground-muted hover:text-primary hover:bg-surface-hover disabled:opacity-50 transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                          >
                            <RotateCw
                              className={`h-3 w-3 ${reissuingId === p.token_id ? "animate-spin" : ""}`}
                              aria-hidden
                            />
                            {reissuingId === p.token_id ? "Reissuing" : "Reissue"}
                          </button>
                          <button
                            onClick={() => setPendingRevokePat(p)}
                            aria-label={`Revoke token ${p.name}`}
                            className="inline-flex items-center gap-1 px-2 h-7 rounded-[var(--radius-sm)] text-xs text-destructive hover:bg-surface-hover transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                          >
                            <Trash2 className="h-3 w-3" aria-hidden />
                            Revoke
                          </button>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>

          {/* Collapsible setup guide */}
          <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden">
            <button
              type="button"
              onClick={toggleSetup}
              aria-expanded={!!setupOpen}
              aria-controls="setup-guide-body"
              className="w-full flex items-center justify-between px-6 py-3 border-b border-border hover:bg-surface-hover cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <span className="coord-ink">Setup guide — 3 steps</span>
              <ChevronRight
                className={`h-4 w-4 transition-transform ${setupOpen ? "rotate-90" : ""}`}
                aria-hidden
              />
            </button>
            {setupOpen && (
              <div id="setup-guide-body" className="p-6 space-y-6">

                {/* STEP 01 — Mint a token */}
                <div>
                  <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
                    <div className="flex items-baseline gap-3">
                      <span className="coord-spark">Step 01</span>
                      <h2 className="text-base font-semibold tracking-tight text-foreground">
                        Mint a token
                      </h2>
                    </div>
                    <span className="coord">Personal Access Token</span>
                  </header>
                  <div className="space-y-3">
                    <p className="text-sm text-foreground-muted leading-relaxed max-w-prose">
                      A Personal Access Token authorizes your agent against the base.
                      You can rotate or revoke it any time.
                    </p>
                    <form onSubmit={handleCreatePAT} className="flex gap-2">
                      <Label htmlFor="new-pat-name" className="sr-only">
                        Token name
                      </Label>
                      <Input
                        id="new-pat-name"
                        placeholder="Token name (e.g. claude-code-macbook)"
                        value={newName}
                        onChange={(e) => setNewName(e.target.value)}
                        aria-invalid={mintError ? true : undefined}
                        className="flex-1"
                      />
                      <Button
                        type="submit"
                        variant="accent"
                        loading={creating}
                        disabled={!newName.trim()}
                      >
                        {!creating && <Plus className="h-4 w-4" aria-hidden />}
                        {creating ? "Minting" : "Mint"}
                      </Button>
                    </form>
                    {mintError && (
                      <Alert variant="destructive">{mintError}</Alert>
                    )}
                  </div>
                </div>

                <div className="border-t border-border" />

                {/* STEP 02 — Drop the snippet */}
                <div>
                  <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
                    <div className="flex items-baseline gap-3">
                      <span className="coord-spark">Step 02</span>
                      <h2 className="text-base font-semibold tracking-tight text-foreground">
                        Drop the snippet
                      </h2>
                    </div>
                    <span className="coord">npm: akb-mcp</span>
                  </header>
                  <div className="space-y-3">
                    <p className="text-sm text-foreground-muted leading-relaxed max-w-prose">
                      Pick your client. Paste once. Your agent learns the base on the
                      next launch.
                    </p>

                    {/* Client picker + snippet — Tabs gives roving tabindex,
                        role=tab/aria-selected, arrow-key nav, and the teal
                        raised-pill active state for free. CodeSnippet supplies
                        the insecure-origin-guarded copy + teal hover. */}
                    <Tabs value={clientTab} onValueChange={(v) => setClientTab(v as ClientTab)}>
                      <TabsList className="flex-wrap">
                        {(Object.keys(MCP_AGENT_LABELS) as ClientTab[]).map((id) => (
                          <TabsTrigger key={id} value={id}>
                            {MCP_AGENT_LABELS[id]}
                          </TabsTrigger>
                        ))}
                      </TabsList>
                      <TabsContent value={clientTab} className="space-y-2">
                        <CodeSnippet
                          code={snippets[clientTab]}
                          filename={MCP_AGENT_FILES[clientTab]}
                        />
                        {clientTab === "cursor" && (
                          <div className="rounded-[var(--radius-md)] border border-border px-4 py-2 text-[11px] font-mono bg-surface-muted text-foreground-muted space-y-0.5">
                            <div><span className="coord mr-2">Cursor</span>~/.cursor/mcp.json</div>
                            <div><span className="coord mr-2">Windsurf</span>~/.codeium/windsurf/mcp_config.json</div>
                            <div><span className="coord mr-2">Gemini</span>~/.gemini/settings.json</div>
                            <div>
                              <span className="coord mr-2">Claude Desktop</span>
                              ~/Library/Application Support/Claude/claude_desktop_config.json{" "}
                              <span className="text-subtle">(macOS)</span>
                            </div>
                          </div>
                        )}
                      </TabsContent>
                    </Tabs>
                    {snippetPat === "<YOUR_PAT>" && (
                      <p className="coord text-foreground-muted">
                        ↑ Replace <span className="text-accent-strong">&lt;YOUR_PAT&gt;</span> with the
                        token string shown after Step 01.
                      </p>
                    )}
                  </div>
                </div>

                <div className="border-t border-border" />

                {/* STEP 03 — Talk to your agent */}
                <div>
                  <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
                    <div className="flex items-baseline gap-3">
                      <span className="coord-spark">Step 03</span>
                      <h2 className="text-base font-semibold tracking-tight text-foreground">
                        Talk to your agent
                      </h2>
                    </div>
                    <Link
                      to="/search?q=AKB+usage+guide"
                      className="coord hover:text-link rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                    >
                      Full guide
                    </Link>
                  </header>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-3 text-sm">
                    <PromptExample
                      text='"Show me how to use AKB with akb_help()"'
                      label="tools + quickstart"
                    />
                    <PromptExample
                      text='"Search the dnotitia vault for the remote-work policy"'
                      label="internal knowledge"
                    />
                    <PromptExample
                      text='"From the sales vault, show deals with win-rate ≥ 60%"'
                      label="data analysis"
                    />
                    <PromptExample
                      text='"Create a todo for Jinwoo: please upload materials"'
                      label="task assignment"
                    />
                  </div>
                </div>

              </div>
            )}
          </div>

        </TabsContent>

        {/* Preferences — status display only; real control lives in header UserMenu */}
        <TabsContent value="preferences" className="pt-6 max-w-4xl space-y-6">
          {/* Theme — status only. Real control lives in the header UserMenu. */}
          <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden">
            <header className="border-b border-border px-6 py-3">
              <span className="coord-ink">Theme</span>
            </header>
            <div className="p-6 flex items-start justify-between gap-4">
              <div>
                <div className="text-sm font-medium text-foreground">
                  Current: <span className="capitalize">{theme}</span>
                  {theme === "system" && (
                    <span className="text-foreground-muted"> (follows OS)</span>
                  )}
                </div>
                <p className="text-xs text-foreground-muted mt-1.5 leading-relaxed">
                  Active mode follows your selection in the header menu.
                </p>
              </div>
              <div className="inline-flex items-center gap-1 text-foreground-muted text-xs">
                <ArrowUpRight className="h-3 w-3" aria-hidden />
                Change in header menu
              </div>
            </div>
          </div>

          {/* Future placeholder card */}
          <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden opacity-50">
            <header className="border-b border-border px-6 py-3">
              <span className="coord-ink">More preferences</span>
            </header>
            <div className="p-6">
              <p className="text-sm text-foreground-muted">
                Language, density, notifications — coming soon.
              </p>
            </div>
          </div>
        </TabsContent>

        {/* The "Memory" tab was removed in v0.5.0 — agent memory now
            lives in a per-user vault (`agent-memory-{username}`) and is
            accessible via the standard /vault/ browse UI. */}

        {/* Admin — user management. Only rendered when user.is_admin. */}
        {user.is_admin && (
          <TabsContent value="admin" className="pt-6 max-w-5xl space-y-4">
            {/* Search + sort bar */}
            <div className="flex flex-wrap items-center gap-3">
              <Input
                placeholder="Search users by name or email…"
                value={adminQuery}
                onChange={(e) => setAdminQuery(e.target.value)}
                className="flex-1 min-w-[260px]"
                aria-label="Search users"
              />
              <Label htmlFor="admin-sort" className="sr-only">Sort</Label>
              <SelectMenu
                id="admin-sort"
                aria-label="Sort users"
                value={adminSort}
                onValueChange={(v) => setAdminSort(v as AdminSort)}
                className="w-auto min-w-[160px]"
                options={[
                  { value: "recent", label: "Recent first" },
                  { value: "oldest", label: "Oldest first" },
                  { value: "username", label: "Username A-Z" },
                  { value: "vaults", label: "Most vaults" },
                ]}
              />
            </div>

            {/* Match count */}
            <div className="coord">
              [{filteredUsers.length} matching of {users?.length ?? 0}]
            </div>

            <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden">
              <div className="p-6">
                {usersError ? (
                  <EmptyState
                    title="Couldn't load users"
                    description="Something went wrong fetching the user list."
                    action={
                      <Button variant="outline" size="sm" onClick={() => loadUsers()}>
                        Retry
                      </Button>
                    }
                  />
                ) : !users ? (
                  <div className="coord" role="status" aria-live="polite">Loading</div>
                ) : filteredUsers.length === 0 ? (
                  <EmptyState
                    title={
                      adminQuery
                        ? `No users matching "${adminQuery}"`
                        : "No users"
                    }
                  />
                ) : (
                  <div className="rounded-[var(--radius-md)] border border-border divide-y divide-border overflow-hidden">
                    {filteredUsers.map((u, i) => (
                      <div
                        key={u.id}
                        data-testid="admin-user-row"
                        className="flex items-center justify-between gap-3 px-4 py-3"
                      >
                        <div className="flex items-baseline gap-3 min-w-0 flex-1">
                          <span className="coord tabular-nums shrink-0">
                            {String(i + 1).padStart(2, "0")}
                          </span>
                          <span
                            data-testid="admin-user-name"
                            title={u.username}
                            className="text-sm font-medium truncate text-foreground"
                          >
                            {u.username}
                          </span>
                          {u.display_name && u.display_name !== u.username && (
                            <span title={u.display_name} className="text-sm text-foreground-muted truncate hidden sm:inline">
                              {u.display_name}
                            </span>
                          )}
                          <code title={u.email} className="font-mono text-[11px] text-foreground-muted truncate hidden md:inline">
                            {u.email}
                          </code>
                          {u.is_admin && (
                            <Badge variant="owner" className="shrink-0">
                              ADMIN
                            </Badge>
                          )}
                        </div>
                        <div className="flex items-center gap-3 shrink-0">
                          <span className="coord tabular-nums hidden sm:inline">
                            Vaults {u.owned_vaults}
                          </span>
                          <span className="coord tabular-nums hidden md:inline">
                            Joined {formatDate(u.created_at)}
                          </span>
                          {u.id === user.user_id ? (
                            <span className="coord text-foreground-muted">
                              Self
                            </span>
                          ) : (
                            <>
                              <button
                                type="button"
                                onClick={() => setResetTarget(u)}
                                title={`Reset password for ${u.username}`}
                                aria-label={`Reset password for ${u.username}`}
                                className="inline-flex items-center gap-1 px-2 h-7 rounded-[var(--radius-sm)] text-xs text-foreground-muted hover:text-primary hover:bg-surface-hover transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                              >
                                <Key className="h-3 w-3" aria-hidden />
                                Reset
                              </button>
                              <button
                                onClick={() => setPendingDeleteUser(u)}
                                disabled={deletingId === u.id}
                                aria-label={`Delete user ${u.username}`}
                                className="inline-flex items-center gap-1 px-2 h-7 rounded-[var(--radius-sm)] text-xs text-destructive hover:bg-surface-hover disabled:opacity-50 transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                              >
                                {deletingId === u.id ? (
                                  <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                                ) : (
                                  <Trash2 className="h-3 w-3" aria-hidden />
                                )}
                                {deletingId === u.id ? "Deleting" : "Delete"}
                              </button>
                            </>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </TabsContent>
        )}
      </Tabs>

      <ConfirmDialog
        open={pendingRevokePat !== null}
        onOpenChange={(o) => !o && setPendingRevokePat(null)}
        title={pendingRevokePat ? `Revoke "${pendingRevokePat.name}"?` : ""}
        description={
          "Any agent currently using this token will lose access immediately.\nThis cannot be undone."
        }
        confirmLabel="Revoke token"
        variant="destructive"
        onConfirm={confirmRevokePat}
      />

      <ConfirmDialog
        open={pendingReissue !== null}
        onOpenChange={(o) => !o && setPendingReissue(null)}
        title={pendingReissue ? `Reissue "${pendingReissue.name}"?` : ""}
        description={
          "The current token stops working the instant this runs — a fresh token is minted to replace it. Any agent still using the old value will lose access until you paste the new one."
        }
        confirmLabel="Reissue token"
        variant="destructive"
        onConfirm={() => {
          if (pendingReissue) return handleReissue(pendingReissue);
        }}
      />

      <ConfirmDialog
        open={pendingDeleteUser !== null}
        onOpenChange={(o) => !o && setPendingDeleteUser(null)}
        title={pendingDeleteUser ? `Delete user "${pendingDeleteUser.username}"?` : ""}
        description={
          pendingDeleteUser && pendingDeleteUser.owned_vaults > 0
            ? `This will ALSO permanently delete ${pendingDeleteUser.owned_vaults} vault(s) they own — including documents, files, tables, and Git history.\n\nThis cannot be undone.`
            : "This cannot be undone."
        }
        confirmLabel="Delete user"
        variant="destructive"
        onConfirm={confirmDeleteUser}
      />

      <AdminResetPasswordDialog
        userId={resetTarget?.id ?? ""}
        username={resetTarget?.username ?? ""}
        open={resetTarget !== null}
        onOpenChange={(o) => {
          if (!o) setResetTarget(null);
        }}
      />
    </div>
  );
}

function PromptExample({ text, label }: { text: string; label: string }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="coord">{label}</div>
      <code className="font-mono text-[13px] text-foreground leading-relaxed">
        {text}
      </code>
    </div>
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
