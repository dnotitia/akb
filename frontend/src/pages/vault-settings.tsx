import { type ReactNode, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle,
  Archive,
  ArrowLeft,
  Globe,
  Lock,
  RotateCcw,
  Save,
  Trash2,
  Unlock,
  type LucideIcon,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import {
  archiveVault,
  getDocument,
  getVaultInfo,
  unarchiveVault,
  updateVault,
} from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { SkillSettingsLink } from "@/components/skill/skill-settings-link";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Segmented } from "@/components/ui/segmented";
import { DeleteVaultDialog } from "@/components/delete-vault-dialog";
import { RoleBadge, VaultStateBadge } from "@/components/status-badge";
import { useVaultHealth } from "@/hooks/use-vault-health";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";

interface TableMeta {
  name: string;
  row_count?: number;
  columns?: Array<{ name: string; type: string }>;
}

interface VaultInfo {
  name: string;
  description?: string;
  role?: "owner" | "admin" | "writer" | "reader";
  status?: string;
  is_archived?: boolean;
  is_external_git?: boolean;
  public_access?: "none" | "reader" | "writer";
  // Identity + scale from get_vault_info (already on the wire) — surfaced so the
  // settings page can answer "who owns it, how old, how big" without a re-fetch.
  owner?: string;
  owner_display_name?: string;
  created_at?: string;
  member_count?: number;
  document_count?: number;
  table_count?: number;
  file_count?: number;
  tables?: TableMeta[];
}

type PublicAccess = "none" | "reader" | "writer";
const PUBLIC_LABELS: Record<PublicAccess, string> = {
  none: "Private",
  reader: "Public · read",
  writer: "Public · write",
};
const PUBLIC_ICONS: Record<PublicAccess, LucideIcon> = {
  none: Lock,
  reader: Globe,
  writer: Unlock,
};
const PUBLIC_DESCRIPTIONS: Record<PublicAccess, string> = {
  none: "Only invited members can see anything in this vault.",
  reader:
    "Any signed-in person with the link can read this vault — including people you never invited. Writes still require an invite.",
  writer:
    "Any signed-in person with the link can read AND write — create, edit, and delete content. Use sparingly.",
};
const PUBLIC_ORDER: PublicAccess[] = ["none", "reader", "writer"];

export default function VaultSettingsPage() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const { refetchVaults } = useVaultRefresh();
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const [loadError, setLoadError] = useState("");
  const [description, setDescription] = useState("");
  const [publicAccess, setPublicAccess] = useState<PublicAccess>("none");
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [saveError, setSaveError] = useState("");
  const [pendingArchive, setPendingArchive] = useState(false);
  const [pendingUnarchive, setPendingUnarchive] = useState(false);
  const [pendingPublicWrite, setPendingPublicWrite] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const vaultHealth = useVaultHealth(name);

  const skillQuery = useQuery({
    queryKey: ["document", name, "overview/vault-skill.md"],
    queryFn: () => getDocument(name!, "overview/vault-skill.md"),
    retry: false,
    enabled: !!name,
  });
  const skillDefined = !skillQuery.isError && !!skillQuery.data;
  const skillUpdatedAt: string | undefined = skillQuery.data?.updated_at;

  function loadInfo(vault: string) {
    setLoadError("");
    getVaultInfo(vault)
      .then((d) => {
        setInfo(d);
        setDescription(d.description || "");
        setPublicAccess((d.public_access as PublicAccess) || "none");
      })
      .catch((e) => setLoadError(e?.message || "Couldn't load this vault."));
  }

  useEffect(() => {
    if (!name) return;
    // Reset stale state from previous param before re-fetch resolves.
    setInfo(null);
    setLoadError("");
    setDescription("");
    setPublicAccess("none");
    setSaveError("");
    loadInfo(name);
  }, [name]);

  // Name the tab/history entry for this page (tab switching + SR orientation).
  useEffect(() => {
    if (!name) return;
    const prev = document.title;
    document.title = `${name} · Settings · AKB`;
    return () => {
      document.title = prev;
    };
  }, [name]);

  const canEdit = info?.role === "owner";
  const dirty = Boolean(
    info &&
      (description !== (info.description || "") ||
        publicAccess !== ((info.public_access as PublicAccess) || "none")),
  );

  // Guard a dirty config behind the browser's unload prompt (refresh / close /
  // external nav), mirroring document-new.tsx. In-app SPA nav has a Discard
  // button + the unsaved hint as its safety net.
  useEffect(() => {
    if (!dirty || saving) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty, saving]);

  // Public-write is the highest-blast-radius change on the page, so unlike the
  // other (gated) lifecycle actions it must clear a destructive confirm before
  // Save commits it. Lowering access or toggling read stays frictionless.
  function requestSave() {
    if (!info) return;
    const enablingPublicWrite =
      publicAccess === "writer" && (info.public_access || "none") !== "writer";
    if (enablingPublicWrite) {
      setPendingPublicWrite(true);
      return;
    }
    void doSave();
  }

  async function doSave() {
    if (!name || !info) return;
    setSaving(true);
    setSaveError("");
    try {
      await updateVault(name, { description, public_access: publicAccess });
      setInfo({ ...info, description, public_access: publicAccess });
      setSavedAt(Date.now());
      setTimeout(() => setSavedAt(null), 2000);
    } catch (e: any) {
      setSaveError(e?.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  function handleDiscard() {
    if (!info) return;
    setDescription(info.description || "");
    setPublicAccess((info.public_access as PublicAccess) || "none");
    setSaveError("");
  }

  async function confirmArchive() {
    if (!name) return;
    await archiveVault(name);
    setInfo(await getVaultInfo(name));
  }
  async function confirmUnarchive() {
    if (!name) return;
    await unarchiveVault(name);
    setInfo(await getVaultInfo(name));
  }

  if (!name) return null;

  const loading = info === null && !loadError;
  const deleteScale = info
    ? ([
        [info.document_count, "document"],
        [info.table_count, "table"],
        [info.file_count, "file"],
      ] as Array<[number | undefined, string]>)
        .filter(([n]) => (n ?? 0) > 0)
        .map(([n, w]) => `${n!.toLocaleString()} ${w}${n === 1 ? "" : "s"}`)
        .join(", ")
    : "";

  return (
    <div className="fade-up max-w-[1100px] mx-auto">
      {loadError && (
        <Alert variant="destructive" className="mb-4">
          {loadError}
          <div className="mt-2">
            <Button variant="outline" size="sm" onClick={() => loadInfo(name)}>
              Try again
            </Button>
          </div>
        </Alert>
      )}

      <div className="flex items-baseline justify-between mb-6 flex-wrap gap-y-2">
        <Link
          to={`/vault/${name}`}
          className="inline-flex items-center gap-1.5 min-h-[36px] coord hover:text-link transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background rounded-[var(--radius-sm)]"
        >
          <ArrowLeft className="h-3 w-3" aria-hidden />
          Back to {name}
        </Link>
        {info?.role && <RoleBadge role={info.role} />}
      </div>

      <div className="coord mb-3">
        Vault · <span className="text-foreground">{name}</span> · Settings
      </div>
      <h1 className="font-display text-3xl tracking-tight text-foreground mb-2">
        Settings
      </h1>
      <p className="text-sm leading-relaxed text-foreground-muted mb-2 max-w-prose">
        Vault metadata, public access, and lifecycle controls.
      </p>

      {/* Identity line — who owns it, how old, how big. From get_vault_info,
          already fetched. Owner is a display name (sans, not mono). */}
      {info &&
        (() => {
          const segs: ReactNode[] = [];
          const owner = info.owner_display_name || info.owner;
          if (owner)
            segs.push(
              <>
                Owned by <span className="text-foreground">{owner}</span>
              </>,
            );
          if (info.created_at) segs.push(<>Created {timeAgo(info.created_at)}</>);
          if (info.member_count != null)
            segs.push(
              <>
                {info.member_count.toLocaleString()} member
                {info.member_count === 1 ? "" : "s"}
              </>,
            );
          if (!segs.length) return null;
          return (
            <div className="coord mb-2 flex flex-wrap items-center gap-x-2 gap-y-1">
              {segs.map((s, i) => (
                <span key={i} className="flex items-center gap-x-2">
                  {i > 0 && <span aria-hidden>·</span>}
                  <span>{s}</span>
                </span>
              ))}
            </div>
          );
        })()}

      <div className="mb-10 min-h-[1.5rem]" aria-busy={loading || undefined}>
        {loading ? (
          <span
            className="inline-block h-5 w-40 rounded bg-surface-muted animate-pulse"
            aria-hidden
          />
        ) : (
          <VaultStateBadge
            archived={info?.is_archived}
            externalGit={info?.is_external_git}
            publicAccess={info?.public_access}
          />
        )}
      </div>
      <span className="sr-only" role="status" aria-live="polite">
        {loading ? "Loading vault settings" : loadError ? "Could not load settings" : ""}
      </span>

      {!canEdit && info && (
        <div
          role="status"
          className="rounded-[var(--radius-md)] border border-border bg-surface-muted px-4 py-2 mb-8 text-xs"
        >
          Read-only view — only the owner can change these settings. Your role: {info.role}.
        </div>
      )}

      {/* § METADATA — the form column is capped for a readable measure; the wide
          content area is for the cards below. */}
      <section className="mb-12" aria-labelledby="meta-h">
        <header className="flex items-baseline gap-3 pb-3 border-b border-border mb-4">
          <h2 id="meta-h" className="coord-ink">
            Metadata
          </h2>
        </header>

        <div className="space-y-5 max-w-2xl">
          <div>
            <Label className="coord-ink mb-1.5 block">Name</Label>
            <Input value={name} disabled />
            <p className="text-xs text-foreground-muted mt-1.5">
              Vault names are immutable. Create a new vault and migrate if you need a rename.
            </p>
          </div>

          <div>
            <Label htmlFor="vault-description" className="coord-ink mb-1.5 block">
              Description
            </Label>
            <Textarea
              id="vault-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              readOnly={!canEdit}
              disabled={saving}
              placeholder="One sentence on what lives in this vault."
              rows={2}
              className="resize-y"
            />
          </div>

          {!skillQuery.isLoading && (
            <SkillSettingsLink vault={name!} defined={skillDefined} updatedAt={skillUpdatedAt} />
          )}

          <div>
            <Label id="public-access-label" className="coord-ink mb-1.5 block">
              Public access
            </Label>
            <Segmented
              aria-labelledby="public-access-label"
              value={publicAccess}
              onChange={(v) => setPublicAccess(v as PublicAccess)}
              disabled={!canEdit || saving}
              className="grid-cols-1 sm:grid-cols-3"
              options={PUBLIC_ORDER.map((v) => {
                const Icon = PUBLIC_ICONS[v];
                return {
                  value: v,
                  label: PUBLIC_LABELS[v],
                  icon: <Icon className="h-3 w-3" aria-hidden />,
                  danger: v === "writer",
                };
              })}
            />
            <p
              className={`text-xs mt-2 leading-relaxed flex items-start gap-1.5 ${
                publicAccess === "writer" ? "text-warning-soft-foreground" : "text-foreground-muted"
              }`}
            >
              {publicAccess === "writer" && (
                <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" aria-hidden />
              )}
              {PUBLIC_DESCRIPTIONS[publicAccess]}
            </p>
          </div>

          {saveError && <Alert variant="destructive">{saveError}</Alert>}

          {canEdit && (
            <div className="flex items-center gap-3">
              <Button onClick={requestSave} loading={saving} disabled={!dirty}>
                {!saving && <Save className="h-4 w-4" aria-hidden />}
                {saving ? "Saving…" : "Save changes"}
              </Button>
              {dirty && !saving && (
                <Button variant="outline" onClick={handleDiscard}>
                  Discard
                </Button>
              )}
              <span role="status" aria-live="polite">
                {savedAt && <span className="coord-spark fade-in">Saved</span>}
              </span>
              {dirty && !savedAt && !saving && (
                <span className="coord">Unsaved changes</span>
              )}
            </div>
          )}
        </div>
      </section>

      {/* § LIFECYCLE */}
      {canEdit && (
        <section aria-labelledby="lifecycle-h" className="mb-12">
          <header className="flex items-baseline gap-3 pb-3 border-b border-border mb-4">
            <h2 id="lifecycle-h" className="coord-ink">
              Lifecycle
            </h2>
          </header>

          <div className="space-y-5">
            <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm p-4">
              <div className="flex items-baseline justify-between flex-wrap gap-y-3">
                <div className="min-w-0 pr-4">
                  <h3 className="text-base font-semibold tracking-tight mb-1">
                    {info?.is_archived ? "Archived" : "Active"}
                  </h3>
                  <p className="text-sm text-foreground-muted leading-relaxed max-w-prose">
                    {info?.is_archived
                      ? "Read-only. Documents and tables can still be browsed and searched, but no writes happen — neither from agents nor from this UI."
                      : "Archive to mark a project as finished. The vault becomes read-only; agents can still recall but cannot write. Reversible."}
                  </p>
                </div>
                {info?.is_archived ? (
                  <Button variant="outline" onClick={() => setPendingUnarchive(true)}>
                    <RotateCcw className="h-4 w-4" aria-hidden />
                    Unarchive
                  </Button>
                ) : (
                  <Button variant="outline" onClick={() => setPendingArchive(true)}>
                    <Archive className="h-4 w-4" aria-hidden />
                    Archive
                  </Button>
                )}
              </div>
            </div>

            <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm p-4">
              <h3 className="text-base font-semibold tracking-tight mb-1">
                Transfer ownership
              </h3>
              <p className="text-sm text-foreground-muted leading-relaxed max-w-prose mb-3">
                You currently own <span className="text-foreground">{name}</span>.
                Reassign ownership to another vault member and you become an admin
                afterward. Use the Members page — it knows who can be promoted.
              </p>
              <Button asChild variant="outline">
                <Link to={`/vault/${name}/members`}>Open members</Link>
              </Button>
            </div>
          </div>
        </section>
      )}

      {/* § DANGER ZONE */}
      {canEdit && (
        <section aria-labelledby="danger-h" className="mb-12">
          <header className="flex items-baseline gap-3 pb-3 border-b border-destructive mb-4">
            <h2 id="danger-h" className="coord-spark text-destructive">
              Danger zone
            </h2>
          </header>

          <div className="rounded-[var(--radius-lg)] border border-destructive/50 bg-destructive/5 p-4">
            <div className="flex items-baseline justify-between flex-wrap gap-y-3">
              <div className="min-w-0 pr-4">
                <h3 className="text-base font-semibold tracking-tight mb-1 text-destructive">
                  Delete vault permanently
                </h3>
                <p className="text-sm text-foreground-muted leading-relaxed max-w-prose">
                  Removes the vault and everything inside it
                  {deleteScale ? <> — <span className="text-foreground">{deleteScale}</span></> : null},
                  plus embeddings, relations, sessions, memories, S3 file objects,
                  and the git repository. Agents lose access immediately. This
                  cannot be undone — prefer Archive if you only need to freeze the
                  vault.
                </p>
              </div>
              <Button variant="destructive" onClick={() => setDeleteOpen(true)}>
                <Trash2 className="h-4 w-4" aria-hidden />
                Delete vault
              </Button>
            </div>
          </div>
        </section>
      )}

      {/* § DIAGNOSTICS — owner-only (non-owners get the indexing summary on the
          overview badge; the raw worker telemetry is operator detail). */}
      {canEdit && vaultHealth && (
        <section aria-labelledby="diag-h">
          <header className="flex items-baseline gap-3 pb-3 border-b border-border mb-4">
            <h2 id="diag-h" className="coord-ink">
              Diagnostics
            </h2>
            <span className="coord">indexing pipeline</span>
          </header>
          <div className="grid grid-cols-2 gap-px rounded-[var(--radius-lg)] overflow-hidden border border-border bg-border shadow-sm">
            <DiagCell title="Indexing" stats={vaultHealth.vector_store?.backfill?.upsert} />
            <DiagCell title="Metadata" stats={vaultHealth.metadata_backfill} />
          </div>
          <p className="text-xs text-foreground-muted mt-2 leading-relaxed max-w-prose">
            Backfill workers process new chunks asynchronously after a write.
            Numbers reset to zero when caught up. Persistent non-zero values
            across multiple refreshes signal a stuck worker — check the
            embedding API or vector-store health.
          </p>
        </section>
      )}

      <ConfirmDialog
        open={pendingArchive}
        onOpenChange={setPendingArchive}
        title={`Archive "${name}"?`}
        description={
          "Documents and tables become read-only. Agents can recall but cannot write.\nYou can unarchive any time."
        }
        confirmLabel="Archive vault"
        onConfirm={confirmArchive}
      />
      <ConfirmDialog
        open={pendingUnarchive}
        onOpenChange={setPendingUnarchive}
        title={`Unarchive "${name}"?`}
        description="The vault returns to active. Agents can write again."
        confirmLabel="Unarchive"
        onConfirm={confirmUnarchive}
      />
      <ConfirmDialog
        open={pendingPublicWrite}
        onOpenChange={setPendingPublicWrite}
        title={`Make "${name}" world-writable?`}
        variant="destructive"
        description={
          <span className="flex items-start gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5 text-destructive" aria-hidden />
            <span>
              Any signed-in person with the link — including people you never
              invited — will be able to create, edit, and delete content in this
              vault. You can lower this again at any time.
            </span>
          </span>
        }
        confirmLabel="Make world-writable"
        onConfirm={doSave}
      />

      <DeleteVaultDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        vault={name}
        onDeleted={() => {
          // Invalidate the sidebar list before navigating so the
          // just-deleted vault doesn't briefly reappear in the picker.
          refetchVaults();
          navigate("/vault");
        }}
      />
    </div>
  );
}

interface DiagStats {
  pending?: number;
  retrying?: number;
  abandoned?: number;
}

function DiagCell({ title, stats }: { title: string; stats?: DiagStats }) {
  return (
    <div className="bg-surface p-3">
      <div className="coord-ink mb-2">{title}</div>
      <dl className="text-xs space-y-1 font-mono tabular-nums">
        <div className="flex justify-between">
          <dt>pending</dt>
          <dd>{stats?.pending ?? "—"}</dd>
        </div>
        <div className="flex justify-between">
          <dt>retrying</dt>
          <dd>{stats?.retrying ?? "—"}</dd>
        </div>
        <div className="flex justify-between text-destructive">
          <dt>abandoned</dt>
          <dd>{stats?.abandoned ?? "—"}</dd>
        </div>
      </dl>
    </div>
  );
}
