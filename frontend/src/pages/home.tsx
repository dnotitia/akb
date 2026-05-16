import { useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  ArrowRight,
  Copy,
  Eye,
  EyeOff,
  File as FileIcon,
  FileText,
  Loader2,
  Plus,
  Table as TableIcon,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/empty-state";
import { RoleBadge } from "@/components/status-badge";
import {
  listVaults,
  getRecent,
  createPAT,
  listPATs,
  revokePAT,
  getVaultInfo,
} from "@/lib/api";
import { timeAgo } from "@/lib/utils";

const MCP_URL = `${window.location.origin}/mcp/`;

type Tab = "claude" | "cursor" | "codex" | "vscode" | "openclaw";

interface VaultRow {
  id: string;
  name: string;
  description?: string;
  role?: "owner" | "admin" | "writer" | "reader";
  status?: string;
}

interface VaultMetrics {
  document_count?: number;
  table_count?: number;
  file_count?: number;
  last_activity?: string;
}

interface RecentRow {
  doc_id: string;
  vault: string;
  path: string;
  title: string;
  commit?: string;
  changed_at?: string;
}

interface PATRow {
  token_id: string;
  name: string;
  prefix: string;
  last_used_at?: string;
}

export default function HomePage() {
  const [vaults, setVaults] = useState<VaultRow[]>([]);
  const [vaultMetrics, setVaultMetrics] = useState<Record<string, VaultMetrics>>({});
  const [recent, setRecent] = useState<RecentRow[]>([]);
  const [pats, setPats] = useState<PATRow[]>([]);
  const [pendingRevoke, setPendingRevoke] = useState<PATRow | null>(null);
  const [activePat, setActivePat] = useState<string | null>(null);
  const [showPat, setShowPat] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [tab, setTab] = useState<Tab>("claude");
  const [vaultFilter, setVaultFilter] = useState("");
  const location = useLocation();

  useEffect(() => {
    listVaults()
      .then((d) => {
        const vs: VaultRow[] = d.vaults || [];
        setVaults(vs);
        // Enrich each vault row with counts + last-activity in parallel.
        // Incremental updates keep the list usable before every call resolves.
        vs.forEach((v) => {
          getVaultInfo(v.name)
            .then((info) =>
              setVaultMetrics((prev) => ({
                ...prev,
                [v.name]: {
                  document_count: info?.document_count,
                  table_count: info?.table_count,
                  file_count: info?.file_count,
                  last_activity: info?.last_activity,
                },
              })),
            )
            .catch(() => {});
        });
      })
      .catch(console.error);
    getRecent(undefined, 8).then((d) => setRecent(d.changes || [])).catch(console.error);
    loadPATs();
  }, []);

  // Scroll to #vaults when the header "Browse" link lands here with that hash.
  useEffect(() => {
    const target = location.hash.slice(1);
    if (target === "vaults" || target === "recent") {
      requestAnimationFrame(() => {
        document
          .getElementById(target)
          ?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
  }, [location.hash]);

  async function loadPATs() {
    try {
      const d = await listPATs();
      setPats(d.tokens || []);
    } catch {
      /* non-fatal: leave pats empty */
    }
  }

  function copy(text: string, label: string) {
    navigator.clipboard.writeText(text);
    setCopied(label);
    setTimeout(() => setCopied(null), 2000);
  }

  async function handleCreatePAT(e: React.FormEvent) {
    e.preventDefault();
    const name = newName.trim() || "agent-token";
    setCreating(true);
    try {
      const r = await createPAT(name);
      setActivePat(r.token);
      setShowPat(true);
      setNewName("");
      loadPATs();
    } catch {
      // surfaced via toast in higher-level error boundary
    }
    setCreating(false);
  }

  const pat = activePat || "<YOUR_PAT>";
  const snippets = useMemo(
    () => ({
      claude: `claude mcp add --scope user akb -- npx akb-mcp --url ${MCP_URL} --pat ${pat} --insecure`,
      cursor: JSON.stringify(
        {
          mcpServers: {
            akb: { command: "npx", args: ["akb-mcp", "--url", MCP_URL, "--pat", pat, "--insecure"] },
          },
        },
        null,
        2,
      ),
      codex: `codex mcp add akb -- npx akb-mcp --url ${MCP_URL} --pat ${pat} --insecure`,
      vscode: JSON.stringify(
        {
          servers: {
            akb: { type: "stdio", command: "npx", args: ["akb-mcp", "--url", MCP_URL, "--pat", pat, "--insecure"] },
          },
        },
        null,
        2,
      ),
      openclaw: JSON.stringify(
        {
          mcp: {
            servers: {
              akb: { command: "npx", args: ["akb-mcp", "--url", MCP_URL, "--pat", pat, "--insecure"] },
            },
          },
        },
        null,
        2,
      ),
    }),
    [pat],
  );

  const q = vaultFilter.trim().toLowerCase();
  const filteredVaults = q
    ? vaults.filter(
        (v) =>
          v.name?.toLowerCase().includes(q) || v.description?.toLowerCase().includes(q),
      )
    : vaults;

  // Main column — Recent + Vaults. Right rail — summary + connect.
  return (
    <div className="fade-up">
      {/* Masthead tagline — editorial signature (matches /auth's
          Fraunces + accent italic treatment), one line so returning
          users pass over it quickly. */}
      <p
        className={
          "font-display-tight mb-10 leading-[1.05] tracking-tight text-foreground " +
          "text-3xl sm:text-4xl lg:text-[40px] lg:mb-12"
        }
      >
        Wire your agent{" "}
        <span className="italic text-accent">into the base</span>.
      </p>

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_320px] gap-x-10 gap-y-10">
      {/* ── Main column ──────────────────────────────────────── */}
      <div className="space-y-10 min-w-0">
        {/* § 01 Recent — top priority, the jump-back-in list. */}
        <section id="recent" className="scroll-mt-24">
          <header className="flex items-baseline gap-3 pb-3 border-b border-border">
            <span className="coord-ink">§ 01</span>
            <h2 className="text-xl font-semibold tracking-tight">Recent activity</h2>
            <span className="coord tabular-nums">[{recent.length}]</span>
          </header>

          {recent.length === 0 ? (
            <EmptyState
              title="Nothing touched yet"
              description="Recent document writes across all your vaults will appear here."
            />
          ) : (
            <ol className="border-x border-b border-border divide-y divide-border">
              {recent.map((c, i) => (
                <li key={c.doc_id + c.changed_at}>
                  <Link
                    to={`/vault/${c.vault}/doc/${c.doc_id}`}
                    className="group grid grid-cols-[40px_120px_1fr_auto] items-baseline gap-4 px-4 py-3 bg-surface hover:bg-surface-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                  >
                    <span className="coord tabular-nums">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <span className="coord font-mono tabular-nums truncate">
                      {c.vault}
                    </span>
                    <div className="min-w-0">
                      <div className="text-sm font-medium tracking-tight truncate text-foreground group-hover:text-accent">
                        {c.title}
                      </div>
                      <div className="coord truncate">{c.path}</div>
                    </div>
                    <span className="coord tabular-nums w-[52px] text-right">
                      {timeAgo(c.changed_at)}
                    </span>
                  </Link>
                </li>
              ))}
            </ol>
          )}
        </section>

        {/* § 02 Vaults — list rows with explicit Open → affordance. */}
        <section id="vaults" className="scroll-mt-24">
          <header className="flex items-baseline justify-between gap-4 flex-wrap pb-3 border-b border-border">
            <div className="flex items-baseline gap-3">
              <span className="coord-ink">§ 02</span>
              <h2 className="text-xl font-semibold tracking-tight">Your vaults</h2>
              <span className="coord tabular-nums">[{vaults.length}]</span>
            </div>
            <div className="flex items-center gap-3">
              {vaults.length > 8 && (
                <Input
                  type="search"
                  placeholder="Filter vaults"
                  value={vaultFilter}
                  onChange={(e) => setVaultFilter(e.target.value)}
                  aria-label="Filter vaults"
                  className="h-9 w-48"
                />
              )}
              <Button asChild variant="outline" size="sm">
                <Link to="/vault/new">
                  <Plus className="h-4 w-4" aria-hidden />
                  New vault
                </Link>
              </Button>
            </div>
          </header>

          {vaults.length === 0 ? (
            <EmptyState
              title="No vaults yet"
              description="Mint a token, then ask your agent to create one — or use the button above."
              action={
                <Button asChild variant="accent" size="sm">
                  <Link to="/vault/new">
                    <Plus className="h-4 w-4" aria-hidden />
                    Create first vault
                  </Link>
                </Button>
              }
            />
          ) : filteredVaults.length === 0 ? (
            <EmptyState title={`No matches for "${vaultFilter}"`} />
          ) : (
            <ol className="border border-border divide-y divide-border">
              {filteredVaults.map((v, i) => {
                const m = vaultMetrics[v.name];
                const lastActivity = m?.last_activity;
                return (
                <li key={v.id}>
                  <Link
                    to={`/vault/${v.name}`}
                    className="group grid grid-cols-[40px_minmax(0,1fr)_auto_auto_auto_24px] items-baseline gap-x-4 gap-y-1 px-4 py-3.5 bg-surface hover:bg-surface-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                  >
                    <span className="coord tabular-nums self-baseline">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <div className="min-w-0 pr-6">
                      <div className="flex items-baseline gap-2 flex-wrap mb-1">
                        <span className="font-mono text-base font-semibold text-foreground group-hover:text-accent transition-colors">
                          {v.name}
                        </span>
                        {v.status === "archived" && (
                          <Badge variant="archived">archived</Badge>
                        )}
                      </div>
                      {v.description && (
                        <p
                          className="text-xs text-foreground-muted leading-relaxed line-clamp-1"
                          title={v.description}
                        >
                          {v.description}
                        </p>
                      )}
                    </div>
                    <VaultStatsCell m={m} />

                    <span className="coord tabular-nums whitespace-nowrap w-[56px] text-right self-baseline">
                      {lastActivity ? timeAgo(lastActivity) : m ? "—" : ""}
                    </span>
                    <div className="shrink-0 self-baseline">
                      {v.role && <RoleBadge role={v.role} />}
                    </div>
                    <ArrowRight
                      className="h-4 w-4 shrink-0 self-baseline text-foreground-muted opacity-40 group-hover:opacity-100 group-hover:translate-x-0.5 group-hover:text-accent transition-all"
                      aria-hidden
                    />
                  </Link>
                </li>
                );
              })}
            </ol>
          )}
        </section>
      </div>

      {/* ── Right rail ───────────────────────────────────────── */}
      <aside className="space-y-8 min-w-0">
        {/* Summary stats */}
        <section className="border border-border bg-surface">
          <div className="border-b border-border px-4 py-2">
            <span className="coord-ink">§ AT A GLANCE</span>
          </div>
          <dl className="divide-y divide-border">
            <RailStat label="VAULTS" value={vaults.length} to="/vault" />
            <RailStat
              label="TOKENS"
              value={pats.length}
              to="/settings?tab=tokens"
            />
            <RailStat label="RECENT" value={recent.length} to="/#recent" />
          </dl>
        </section>

        {/* Connect — always open. Mint + snippet tabs kept; prompt examples
            moved to docs-territory since they're one-time guidance. */}
        <section className="border border-border bg-surface">
          <div className="flex items-baseline justify-between gap-2 px-4 py-3">
            <span className="coord-ink">§ CONNECT</span>
            <div className="flex items-baseline gap-3">
              {pats.length > 0 ? (
                <span className="coord tabular-nums">[{pats.length}]</span>
              ) : (
                <Badge variant="pending">needs setup</Badge>
              )}
              <Link
                to="/settings?tab=tokens"
                className="coord hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
              >
                ↗ MANAGE
              </Link>
            </div>
          </div>

          <div className="border-t border-border">
              {/* Mint */}
              <div className="p-4 border-b border-border">
                <div className="coord-spark mb-2">MINT TOKEN</div>
                <form onSubmit={handleCreatePAT} className="space-y-2">
                  <Label htmlFor="pat-name" className="sr-only">Token name</Label>
                  <Input
                    id="pat-name"
                    type="text"
                    placeholder="Token name (e.g. my-laptop)"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    className="h-8 text-xs"
                  />
                  <Button
                    type="submit"
                    disabled={creating}
                    variant="accent"
                    size="sm"
                    className="w-full"
                  >
                    {creating ? (
                      <>
                        <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                        Minting…
                      </>
                    ) : (
                      <>
                        <Plus className="h-3 w-3" aria-hidden />
                        Mint token
                      </>
                    )}
                  </Button>
                </form>

                {activePat && (
                  <div className="mt-3 border border-accent bg-accent/5 p-2">
                    <div className="coord-spark mb-1">FRESH — COPY NOW</div>
                    <div className="flex items-center gap-1.5">
                      <code className="flex-1 font-mono text-[10px] text-foreground break-all leading-snug">
                        {showPat ? activePat : activePat.slice(0, 10) + "•".repeat(14)}
                      </code>
                      <button
                        onClick={() => setShowPat(!showPat)}
                        aria-label={showPat ? "Hide token" : "Show token"}
                        className="coord hover:text-accent cursor-pointer shrink-0"
                      >
                        {showPat ? (
                          <EyeOff className="h-3 w-3" aria-hidden />
                        ) : (
                          <Eye className="h-3 w-3" aria-hidden />
                        )}
                      </button>
                      <button
                        onClick={() => copy(activePat, "pat")}
                        aria-label="Copy token"
                        className="coord hover:text-accent cursor-pointer shrink-0"
                      >
                        {copied === "pat" ? "OK" : <Copy className="h-3 w-3" aria-hidden />}
                      </button>
                    </div>
                  </div>
                )}
              </div>

              {/* Snippet — compact tabs, one panel. */}
              <div className="p-4 border-b border-border">
                <div className="coord-spark mb-2">DROP SNIPPET</div>
                <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
                  <TabsList className="flex-wrap gap-0 mb-2">
                    <TabsTrigger value="claude" className="px-2 py-1 text-[10px]">Claude Code</TabsTrigger>
                    <TabsTrigger value="cursor" className="px-2 py-1 text-[10px]">Cursor</TabsTrigger>
                    <TabsTrigger value="codex" className="px-2 py-1 text-[10px]">Codex</TabsTrigger>
                    <TabsTrigger value="vscode" className="px-2 py-1 text-[10px]">VS Code</TabsTrigger>
                    <TabsTrigger value="openclaw" className="px-2 py-1 text-[10px]">OpenClaw</TabsTrigger>
                  </TabsList>
                  <TabsContent value={tab}>
                    <div className="border border-border">
                      <div className="flex items-center justify-between border-b border-border bg-foreground text-background px-2 py-1">
                        <span className="font-mono text-[9px] uppercase tracking-wider truncate">
                          {tab === "claude" && "TERMINAL"}
                          {tab === "cursor" && "mcp.json"}
                          {tab === "codex" && "TERMINAL"}
                          {tab === "vscode" && "mcp.json"}
                          {tab === "openclaw" && "openclaw.json"}
                        </span>
                        <button
                          onClick={() => copy(snippets[tab], tab)}
                          aria-label="Copy snippet"
                          className={`font-mono text-[9px] uppercase tracking-wider cursor-pointer shrink-0 ${
                            copied === tab ? "text-accent" : "hover:text-accent"
                          }`}
                        >
                          {copied === tab ? "✓" : "COPY"}
                        </button>
                      </div>
                      <pre className="font-mono text-[10px] leading-snug p-2 overflow-x-auto bg-surface text-foreground whitespace-pre-wrap break-all">
                        {snippets[tab]}
                      </pre>
                    </div>
                  </TabsContent>
                </Tabs>
              </div>

              {/* Active tokens — one-line rows; manage link sits in the
                  Connect section header to keep related actions together. */}
              <div className="p-4">
                <div className="coord-spark mb-2">ACTIVE TOKENS</div>
                {pats.length === 0 ? (
                  <div className="coord">— none —</div>
                ) : (
                  <ul className="divide-y divide-border border border-border">
                    {pats.slice(0, 4).map((p) => (
                      <li
                        key={p.token_id}
                        className="flex items-center justify-between gap-2 px-2 py-1.5 text-xs"
                      >
                        <span className="truncate text-foreground font-medium">{p.name}</span>
                        <div className="flex items-center gap-2 shrink-0">
                          <span className="coord tabular-nums">
                            {p.last_used_at ? timeAgo(p.last_used_at) : "—"}
                          </span>
                          <button
                            onClick={() => setPendingRevoke(p)}
                            aria-label={`Revoke token ${p.name}`}
                            className="text-foreground-muted hover:text-destructive transition-colors cursor-pointer"
                          >
                            <Trash2 className="h-3 w-3" aria-hidden />
                          </button>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
          </div>
        </section>
      </aside>
      </div>

      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(o) => !o && setPendingRevoke(null)}
        title={pendingRevoke ? `Revoke "${pendingRevoke.name}"?` : ""}
        description={
          "Any agent currently using this token will lose access immediately.\nThis cannot be undone."
        }
        confirmLabel="Revoke token"
        variant="destructive"
        onConfirm={async () => {
          if (!pendingRevoke) return;
          await revokePAT(pendingRevoke.token_id);
          await loadPATs();
        }}
      />
    </div>
  );
}

/**
 * Compact stats cell: icon + count per non-empty category. Blank while
 * metrics are loading; "—" when the vault has no content. Icons replace
 * the "DOCS / TABLES / FILES" labels to keep the cell narrow and scannable.
 */
function VaultStatsCell({ m }: { m?: VaultMetrics }) {
  if (!m) {
    return (
      <span
        className="coord tabular-nums whitespace-nowrap self-baseline"
        aria-hidden
      />
    );
  }
  const d = m.document_count ?? 0;
  const t = m.table_count ?? 0;
  const f = m.file_count ?? 0;
  const title = `${d} document${d === 1 ? "" : "s"} · ${t} table${t === 1 ? "" : "s"} · ${f} file${f === 1 ? "" : "s"}`;
  if (d + t + f === 0) {
    return (
      <span
        className="coord tabular-nums whitespace-nowrap self-baseline"
        title={title}
      >
        —
      </span>
    );
  }
  return (
    <span
      className="coord tabular-nums whitespace-nowrap self-baseline inline-flex items-center gap-2"
      title={title}
    >
      {d > 0 && (
        <span className="inline-flex items-center gap-1">
          <FileText className="h-3 w-3" aria-hidden />
          {d}
        </span>
      )}
      {t > 0 && (
        <span className="inline-flex items-center gap-1">
          <TableIcon className="h-3 w-3" aria-hidden />
          {t}
        </span>
      )}
      {f > 0 && (
        <span className="inline-flex items-center gap-1">
          <FileIcon className="h-3 w-3" aria-hidden />
          {f}
        </span>
      )}
    </span>
  );
}

function RailStat({
  label,
  value,
  to,
}: {
  label: string;
  value: number;
  to?: string;
}) {
  const body = (
    <>
      <dt className="coord group-hover:text-accent transition-colors">{label}</dt>
      <dd className="font-mono text-xl font-semibold tabular-nums text-foreground group-hover:text-accent transition-colors">
        {String(value).padStart(2, "0")}
      </dd>
    </>
  );
  if (to) {
    return (
      <Link
        to={to}
        className="group flex items-baseline justify-between px-4 py-3 hover:bg-surface-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
      >
        {body}
      </Link>
    );
  }
  return (
    <div className="flex items-baseline justify-between px-4 py-3">{body}</div>
  );
}
