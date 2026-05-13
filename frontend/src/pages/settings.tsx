import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  ArrowLeft,
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
  type AdminUser,
} from "@/lib/api";
import { AdminResetPasswordDialog } from "@/components/admin-reset-password-dialog";
import { formatDate } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/empty-state";
import { MemoryTab } from "@/components/memory-tab";
import { ThemeToggle } from "@/components/theme-toggle";
import { useTheme } from "@/hooks/use-theme";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";

const MCP_URL = `${window.location.origin}/mcp/`;

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

type TabId = "profile" | "tokens" | "preferences" | "memory" | "admin";
type ClientTab = "claude" | "cursor" | "codex" | "vscode" | "openclaw";

export default function SettingsPage() {
  const [user, setUser] = useState<User | null>(null);
  const [pats, setPats] = useState<PAT[]>([]);
  const [newName, setNewName] = useState("");
  const [newPat, setNewPat] = useState<string | null>(null);
  const [showPat, setShowPat] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [clientTab, setClientTab] = useState<ClientTab>("claude");
  const [pendingDeleteUser, setPendingDeleteUser] = useState<AdminUser | null>(null);
  const [pendingRevokePat, setPendingRevokePat] = useState<PAT | null>(null);
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null);
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwConfirm, setPwConfirm] = useState("");
  const [pwError, setPwError] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwOk, setPwOk] = useState(false);
  const { theme } = useTheme();

  async function handleChangePassword(e: React.FormEvent) {
    e.preventDefault();
    setPwError("");
    setPwOk(false);
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
      setPwOk(true);
      setPwCurrent("");
      setPwNew("");
      setPwConfirm("");
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
        if (u?.is_admin) loadUsers();
      })
      .catch(() => {
        location.href = "/auth";
      });
    loadPATs();
  }, []);

  async function loadPATs() {
    const d = await listPATs();
    setPats(d.tokens || []);
  }

  async function loadUsers() {
    const d = await adminListUsers();
    setUsers(d.users || []);
  }

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

  function copy(text: string, label: string) {
    navigator.clipboard.writeText(text);
    setCopied(label);
    setTimeout(() => setCopied(null), 2000);
  }

  async function handleCreatePAT(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const r = await createPAT(newName);
      setNewPat(r.token);
      setNewName("");
      loadPATs();
    } finally {
      setCreating(false);
    }
  }

  const stdioConfig = (pat: string) =>
    JSON.stringify(
      {
        mcpServers: {
          akb: {
            command: "npx",
            args: ["akb-mcp", "--url", MCP_URL, "--pat", pat, "--insecure"],
          },
        },
      },
      null,
      2,
    );

  // Pat used in snippets: prefer fresh mint, else first active, else placeholder
  const snippetPat = newPat || pats[0]?.prefix + "…" || "<YOUR_PAT>";
  const snippets = useMemo<Record<ClientTab, string>>(
    () => ({
      claude: `claude mcp add --scope user akb -- npx akb-mcp --url ${MCP_URL} --pat ${snippetPat} --insecure`,
      cursor: JSON.stringify(
        {
          mcpServers: {
            akb: {
              command: "npx",
              args: ["akb-mcp", "--url", MCP_URL, "--pat", snippetPat, "--insecure"],
            },
          },
        },
        null,
        2,
      ),
      codex: `codex mcp add akb -- npx akb-mcp --url ${MCP_URL} --pat ${snippetPat} --insecure`,
      vscode: JSON.stringify(
        {
          servers: {
            akb: {
              type: "stdio",
              command: "npx",
              args: ["akb-mcp", "--url", MCP_URL, "--pat", snippetPat, "--insecure"],
            },
          },
        },
        null,
        2,
      ),
      openclaw: JSON.stringify(
        {
          mcp: {
            servers: {
              akb: {
                command: "npx",
                args: ["akb-mcp", "--url", MCP_URL, "--pat", snippetPat, "--insecure"],
              },
            },
          },
        },
        null,
        2,
      ),
    }),
    [snippetPat],
  );

  if (!user) return null;

  // Active tab synced to `?tab=` so Profile/Tokens/etc. are deep-linkable.
  // `admin` is only a valid value when the viewer is an admin — otherwise
  // it falls back to the default so non-admins can't land on a blank pane.
  const allowedTabs: TabId[] = ["profile", "tokens", "preferences", "memory"];
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
          className="inline-flex items-center gap-1.5 coord hover:text-accent transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <ArrowLeft className="h-3 w-3" aria-hidden />
          BACK
        </button>
        <nav aria-label="Breadcrumb" className="flex items-center gap-2 coord">
          <Link to="/" className="hover:text-accent">HOME</Link>
          <ChevronRight className="h-3 w-3 text-foreground-muted" aria-hidden />
          <span className="text-foreground">SETTINGS</span>
        </nav>
      </div>

      <header className="mb-6">
        <div className="coord-spark mb-2">§ SETTINGS</div>
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">
          Settings
        </h1>
      </header>

      <Tabs value={activeTab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="profile">Profile</TabsTrigger>
          <TabsTrigger value="tokens" className="gap-1.5">
            Tokens
            <span className="coord tabular-nums">[{pats.length}]</span>
          </TabsTrigger>
          <TabsTrigger value="preferences">Preferences</TabsTrigger>
          <TabsTrigger value="memory">Memory</TabsTrigger>
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
          <div className="border border-border bg-surface">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-4 p-6">
              <ReadOnlyField label="USERNAME" value={user.username} />
              <ReadOnlyField label="EMAIL" value={user.email} />
              <ReadOnlyField
                label="DISPLAY NAME"
                value={user.display_name || "—"}
              />
              <ReadOnlyField
                label="ROLE"
                value={user.is_admin ? "ADMIN" : "USER"}
                accent={user.is_admin}
              />
            </div>
          </div>

          <section
            className="space-y-3 pt-6 border-t border-border"
            aria-labelledby="change-pw-heading"
          >
            <h2 id="change-pw-heading" className="coord-ink">
              CHANGE PASSWORD
            </h2>
            <form
              onSubmit={handleChangePassword}
              className="space-y-3 max-w-md"
            >
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
                  required
                />
              </div>
              <div>
                <Label htmlFor="pw-confirm">Confirm new password</Label>
                <Input
                  id="pw-confirm"
                  type="password"
                  autoComplete="new-password"
                  value={pwConfirm}
                  onChange={(e) => setPwConfirm(e.target.value)}
                  required
                />
              </div>
              {pwError && (
                <p role="alert" className="text-destructive text-xs font-mono">
                  {pwError}
                </p>
              )}
              {pwOk && (
                <p className="text-foreground text-xs font-mono">
                  Password changed.
                </p>
              )}
              <Button type="submit" disabled={pwBusy}>
                {pwBusy && (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                )}
                Change password
              </Button>
            </form>
          </section>
        </TabsContent>

        {/* Tokens — PATs + fresh token banner when minted */}
        <TabsContent value="tokens" className="pt-6 space-y-4 max-w-4xl">
          {newPat && (
            <section className="border border-accent bg-accent/5">
              <div className="border-b border-accent px-4 py-2 flex items-baseline justify-between">
                <span className="coord-spark">⊛ FRESH TOKEN — COPY NOW</span>
                <button
                  onClick={() => setNewPat(null)}
                  aria-label="Dismiss fresh token"
                  className="coord hover:text-destructive cursor-pointer"
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              </div>
              <div className="p-6 space-y-4">
                <div className="flex items-center gap-3">
                  <code className="flex-1 font-mono text-xs text-foreground break-all border border-border px-3 py-2 bg-surface">
                    {showPat ? newPat : newPat.slice(0, 12) + "•".repeat(20)}
                  </code>
                  <button
                    onClick={() => setShowPat(!showPat)}
                    aria-label={showPat ? "Hide token" : "Show token"}
                    className="coord hover:text-accent cursor-pointer"
                  >
                    {showPat ? (
                      <EyeOff className="h-3 w-3" aria-hidden />
                    ) : (
                      <Eye className="h-3 w-3" aria-hidden />
                    )}
                  </button>
                  <button
                    onClick={() => copy(newPat, "pat")}
                    aria-label="Copy token"
                    className="coord hover:text-accent cursor-pointer"
                  >
                    {copied === "pat" ? "✓ COPIED" : <Copy className="h-3 w-3" aria-hidden />}
                  </button>
                </div>

                <div className="border border-border">
                  <div className="border-b border-border bg-foreground text-background px-3 py-1.5 flex items-center justify-between">
                    <span className="font-mono text-[10px] uppercase tracking-wider">
                      CURSOR / WINDSURF — settings.json
                    </span>
                    <button
                      onClick={() => copy(stdioConfig(newPat), "stdio")}
                      aria-label="Copy config"
                      className={`font-mono text-[10px] uppercase tracking-wider cursor-pointer ${
                        copied === "stdio" ? "text-accent" : "hover:text-accent"
                      }`}
                    >
                      {copied === "stdio" ? "✓ COPIED" : "COPY"}
                    </button>
                  </div>
                  <pre className="text-[11px] font-mono p-3 overflow-x-auto bg-surface text-foreground whitespace-pre-wrap break-all">
                    {stdioConfig(newPat)}
                  </pre>
                </div>
              </div>
            </section>
          )}

          {/* Active tokens — primary content on this tab (management). */}
          <section className="border border-border bg-surface">
            <header className="border-b border-border px-6 py-3 flex items-baseline gap-3">
              <span className="coord-ink">§ ACTIVE TOKENS</span>
              <span className="coord tabular-nums">[{pats.length}]</span>
            </header>
            <div className="p-6">
              {pats.length === 0 ? (
                <EmptyState title="No tokens yet — mint one below." />
              ) : (
                <div className="border border-border divide-y divide-border">
                  {pats.map((p, i) => (
                    <div
                      key={p.token_id}
                      className="flex items-center justify-between gap-3 px-4 py-3"
                    >
                      <div className="flex items-baseline gap-3 min-w-0">
                        <span className="coord tabular-nums">
                          {String(i + 1).padStart(2, "0")}
                        </span>
                        <span className="text-sm font-medium truncate text-foreground">
                          {p.name}
                        </span>
                        <code className="font-mono text-[11px] text-foreground-muted">
                          {p.prefix}••••
                        </code>
                      </div>
                      <div className="flex items-center gap-3 shrink-0">
                        <span className="coord tabular-nums hidden sm:inline">
                          CREATED {formatDate(p.created_at).toUpperCase()}
                        </span>
                        {p.last_used_at && (
                          <span className="coord tabular-nums hidden md:inline">
                            USED {formatDate(p.last_used_at).toUpperCase()}
                          </span>
                        )}
                        <button
                          onClick={async () => {
                            await revokePAT(p.token_id);
                            const r = await createPAT(p.name);
                            setNewPat(r.token);
                            loadPATs();
                          }}
                          aria-label={`Reissue token ${p.name}`}
                          className="inline-flex items-center gap-1 text-xs font-mono uppercase tracking-wider text-foreground-muted hover:text-accent transition-colors cursor-pointer"
                        >
                          <RotateCw className="h-3 w-3" aria-hidden />
                          Reissue
                        </button>
                        <button
                          onClick={() => setPendingRevokePat(p)}
                          aria-label={`Revoke token ${p.name}`}
                          className="inline-flex items-center gap-1 text-xs font-mono uppercase tracking-wider text-foreground-muted hover:text-destructive transition-colors cursor-pointer"
                        >
                          <Trash2 className="h-3 w-3" aria-hidden />
                          Revoke
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>

          {/* ── Setup guide — onboarding reference below ──────────── */}
          <div className="coord mt-2 pt-4 border-t border-border">
            § Setup guide — mint a new token and wire it into a client
          </div>

          {/* STEP 01 — Mint a token */}
          <section className="border border-border bg-surface">
            <header className="border-b border-border px-6 py-3 flex items-baseline justify-between flex-wrap gap-2">
              <div className="flex items-baseline gap-3">
                <span className="coord-spark">STEP 01</span>
                <h2 className="text-base font-semibold tracking-tight text-foreground">
                  Mint a token
                </h2>
              </div>
              <span className="coord">Personal Access Token</span>
            </header>
            <div className="p-6 space-y-3">
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
                  className="flex-1"
                />
                <Button
                  type="submit"
                  variant="accent"
                  disabled={creating || !newName.trim()}
                >
                  {creating ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                      Minting
                    </>
                  ) : (
                    <>
                      <Plus className="h-4 w-4" aria-hidden />
                      Mint
                    </>
                  )}
                </Button>
              </form>
            </div>
          </section>

          {/* STEP 02 — Drop the snippet */}
          <section className="border border-border bg-surface">
            <header className="border-b border-border px-6 py-3 flex items-baseline justify-between flex-wrap gap-2">
              <div className="flex items-baseline gap-3">
                <span className="coord-spark">STEP 02</span>
                <h2 className="text-base font-semibold tracking-tight text-foreground">
                  Drop the snippet
                </h2>
              </div>
              <span className="coord">npm: akb-mcp</span>
            </header>
            <div className="p-6 space-y-3">
              <p className="text-sm text-foreground-muted leading-relaxed max-w-prose">
                Pick your client. Paste once. Your agent learns the base on the
                next launch.
              </p>

              {/* Client tabs */}
              <div className="flex flex-wrap border border-border">
                {(
                  [
                    ["claude", "01", "Claude Code"],
                    ["cursor", "02", "Cursor / Windsurf / Gemini / Claude Desktop"],
                    ["codex", "03", "Codex CLI"],
                    ["vscode", "04", "VS Code"],
                    ["openclaw", "05", "OpenClaw"],
                  ] as [ClientTab, string, string][]
                ).map(([id, num, label], i) => (
                  <button
                    key={id}
                    type="button"
                    onClick={() => setClientTab(id)}
                    className={`flex-1 min-w-[140px] text-left px-3 py-2 transition-colors ${
                      i > 0 ? "border-l border-border" : ""
                    } ${
                      clientTab === id
                        ? "bg-foreground text-background"
                        : "hover:bg-surface-muted cursor-pointer"
                    }`}
                  >
                    <div
                      className={`coord ${clientTab === id ? "text-background" : ""}`}
                    >
                      {num}
                    </div>
                    <div className="text-xs font-medium tracking-tight">
                      {label}
                    </div>
                  </button>
                ))}
              </div>

              {/* Snippet */}
              <div className="border border-border">
                <div className="flex items-center justify-between border-b border-border bg-foreground text-background px-3 py-1.5">
                  <span className="font-mono text-[10px] uppercase tracking-wider truncate">
                    {clientTab === "claude" && "TERMINAL"}
                    {clientTab === "cursor" && "mcpServers schema — per-client path below"}
                    {clientTab === "codex" && "TERMINAL"}
                    {clientTab === "vscode" && ".vscode/mcp.json"}
                    {clientTab === "openclaw" && "~/.openclaw/openclaw.json"}
                  </span>
                  <button
                    type="button"
                    onClick={() => copy(snippets[clientTab], clientTab)}
                    aria-label="Copy snippet"
                    className={`font-mono text-[10px] uppercase tracking-wider cursor-pointer shrink-0 ${
                      copied === clientTab ? "text-accent" : "hover:text-accent"
                    }`}
                  >
                    {copied === clientTab ? "✓ COPIED" : "COPY"}
                  </button>
                </div>
                <pre className="font-mono text-[11px] leading-relaxed p-4 overflow-x-auto bg-surface text-foreground whitespace-pre-wrap break-all">
                  {snippets[clientTab]}
                </pre>
                {clientTab === "cursor" && (
                  <div className="border-t border-border px-4 py-2 text-[11px] font-mono bg-surface-muted text-foreground-muted space-y-0.5">
                    <div><span className="coord mr-2">CURSOR</span>~/.cursor/mcp.json</div>
                    <div><span className="coord mr-2">WINDSURF</span>~/.codeium/windsurf/mcp_config.json</div>
                    <div><span className="coord mr-2">GEMINI</span>~/.gemini/settings.json</div>
                    <div>
                      <span className="coord mr-2">CLAUDE DESKTOP</span>
                      ~/Library/Application Support/Claude/claude_desktop_config.json{" "}
                      <span className="text-foreground-muted/60">(macOS)</span>
                    </div>
                  </div>
                )}
              </div>
              {snippetPat === "<YOUR_PAT>" && (
                <p className="coord text-foreground-muted">
                  ↑ Replace <span className="text-accent">&lt;YOUR_PAT&gt;</span> with the
                  token string shown after Step 01.
                </p>
              )}
            </div>
          </section>

          {/* STEP 03 — Talk to your agent */}
          <section className="border border-border bg-surface">
            <header className="border-b border-border px-6 py-3 flex items-baseline justify-between flex-wrap gap-2">
              <div className="flex items-baseline gap-3">
                <span className="coord-spark">STEP 03</span>
                <h2 className="text-base font-semibold tracking-tight text-foreground">
                  Talk to your agent
                </h2>
              </div>
              <Link
                to="/search?q=AKB+usage+guide"
                className="coord hover:text-accent"
              >
                ↗ FULL GUIDE
              </Link>
            </header>
            <div className="p-6">
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
          </section>

        </TabsContent>

        {/* Preferences — theme only for now */}
        <TabsContent value="preferences" className="pt-6 max-w-4xl">
          <div className="border border-border bg-surface">
            <div className="p-6 flex items-center justify-between gap-4">
              <div>
                <div className="text-sm font-medium text-foreground">Theme</div>
                <div className="text-xs text-foreground-muted mt-1">
                  Current: <span className="font-mono uppercase">{theme}</span>
                  {theme === "system" && " (follows OS preference)"}
                </div>
              </div>
              <ThemeToggle />
            </div>
          </div>
        </TabsContent>

        {/* Memory — agent-stored facts the user can audit + forget. */}
        <TabsContent value="memory" className="pt-6 max-w-4xl">
          <MemoryTab />
        </TabsContent>

        {/* Admin — user management. Only rendered when user.is_admin. */}
        {user.is_admin && (
          <TabsContent value="admin" className="pt-6 max-w-5xl">
            <div className="border border-border bg-surface">
              <div className="p-6">
                {!users ? (
                  <div className="coord">LOADING…</div>
                ) : users.length === 0 ? (
                  <EmptyState title="No users" />
                ) : (
                  <div className="border border-border divide-y divide-border">
                    {users.map((u, i) => (
                      <div
                        key={u.id}
                        className="flex items-center justify-between gap-3 px-4 py-3"
                      >
                        <div className="flex items-baseline gap-3 min-w-0 flex-1">
                          <span className="coord tabular-nums shrink-0">
                            {String(i + 1).padStart(2, "0")}
                          </span>
                          <span className="text-sm font-medium truncate text-foreground">
                            {u.username}
                          </span>
                          {u.display_name && u.display_name !== u.username && (
                            <span className="text-sm text-foreground-muted truncate hidden sm:inline">
                              {u.display_name}
                            </span>
                          )}
                          <code className="font-mono text-[11px] text-foreground-muted truncate hidden md:inline">
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
                            VAULTS {u.owned_vaults}
                          </span>
                          <span className="coord tabular-nums hidden md:inline">
                            JOINED {formatDate(u.created_at).toUpperCase()}
                          </span>
                          {u.id === user.user_id ? (
                            <span className="coord text-foreground-muted">
                              — SELF —
                            </span>
                          ) : (
                            <>
                              <button
                                type="button"
                                onClick={() => setResetTarget(u)}
                                title={`Reset password for ${u.username}`}
                                aria-label={`Reset password for ${u.username}`}
                                className="inline-flex items-center gap-1 text-xs font-mono uppercase tracking-wider text-foreground-muted hover:text-accent transition-colors cursor-pointer"
                              >
                                <Key className="h-3 w-3" aria-hidden />
                                Reset
                              </button>
                              <button
                                onClick={() => setPendingDeleteUser(u)}
                                disabled={deletingId === u.id}
                                aria-label={`Delete user ${u.username}`}
                                className="inline-flex items-center gap-1 text-xs font-mono uppercase tracking-wider text-foreground-muted hover:text-destructive disabled:opacity-40 transition-colors cursor-pointer"
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

function ReadOnlyField({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div>
      <div className="coord mb-1">{label}</div>
      <div
        className={`text-sm font-medium ${accent ? "text-accent" : "text-foreground"}`}
      >
        {value}
      </div>
    </div>
  );
}
