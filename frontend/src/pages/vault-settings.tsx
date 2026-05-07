import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Archive, ArrowLeft, Globe, Lock, RotateCcw, Save, Trash2 } from "lucide-react";
import {
  archiveVault,
  getVaultInfo,
  unarchiveVault,
  updateVault,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DeleteVaultDialog } from "@/components/delete-vault-dialog";
import { RoleBadge, VaultStateBadge } from "@/components/status-badge";
import { useVaultHealth } from "@/hooks/use-vault-health";

interface VaultInfo {
  name: string;
  description?: string;
  role?: "owner" | "admin" | "writer" | "reader";
  status?: string;
  is_archived?: boolean;
  is_external_git?: boolean;
  public_access?: "none" | "reader" | "writer";
}

type PublicAccess = "none" | "reader" | "writer";
const PUBLIC_LABELS: Record<PublicAccess, string> = {
  none: "Private",
  reader: "Public · read",
  writer: "Public · write",
};
const PUBLIC_DESCRIPTIONS: Record<PublicAccess, string> = {
  none: "Only invited members can see anything in this vault.",
  reader: "Anyone with the URL can read this vault. Writes still require an invite.",
  writer: "Anyone with the URL can read AND write. Use sparingly.",
};

export default function VaultSettingsPage() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const [description, setDescription] = useState("");
  const [publicAccess, setPublicAccess] = useState<PublicAccess>("none");
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [pendingArchive, setPendingArchive] = useState(false);
  const [pendingUnarchive, setPendingUnarchive] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const vaultHealth = useVaultHealth(name);

  useEffect(() => {
    if (!name) return;
    getVaultInfo(name)
      .then((d) => {
        setInfo(d);
        setDescription(d.description || "");
        setPublicAccess((d.public_access as PublicAccess) || "none");
      })
      .catch((e) => setError(e?.message || "Failed to load"));
  }, [name]);

  const canEdit = info?.role === "owner";
  const dirty =
    info &&
    (description !== (info.description || "") ||
      publicAccess !== ((info.public_access as PublicAccess) || "none"));

  async function handleSave() {
    if (!name || !info) return;
    setSaving(true);
    setError("");
    try {
      await updateVault(name, { description, public_access: publicAccess });
      setInfo({ ...info, description, public_access: publicAccess });
      setSavedAt(Date.now());
      setTimeout(() => setSavedAt(null), 2000);
    } catch (e: any) {
      setError(e?.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function confirmArchive() {
    if (!name) return;
    await archiveVault(name);
    const fresh = await getVaultInfo(name);
    setInfo(fresh);
  }
  async function confirmUnarchive() {
    if (!name) return;
    await unarchiveVault(name);
    const fresh = await getVaultInfo(name);
    setInfo(fresh);
  }

  if (!name) return null;

  return (
    <div className="fade-up max-w-[1280px] mx-auto">
      <div className="flex items-baseline justify-between mb-6 flex-wrap gap-y-2">
        <Link
          to={`/vault/${name}`}
          className="inline-flex items-center gap-1.5 coord hover:text-accent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <ArrowLeft className="h-3 w-3" aria-hidden />
          BACK TO {name.toUpperCase()}
        </Link>
        {info?.role && <RoleBadge role={info.role} />}
      </div>

      <div className="coord mb-3">VAULT · {name.toUpperCase()} · SETTINGS</div>
      <h1 className="font-serif text-[44px] leading-[0.95] tracking-[-0.03em] text-foreground mb-2">
        Settings<span className="text-foreground-muted">.</span>
      </h1>
      <p className="font-serif-italic text-[16px] leading-[1.55] text-foreground-muted mb-2 max-w-prose">
        Vault metadata, public access, and lifecycle controls.
      </p>
      <div className="mb-10">
        <VaultStateBadge
          archived={info?.is_archived}
          externalGit={info?.is_external_git}
          publicAccess={info?.public_access}
        />
      </div>

      {!canEdit && info && (
        <div role="status" className="border border-border bg-surface-muted px-4 py-2 mb-8 text-xs">
          Read-only view — only the owner can change these settings. Your role: {info.role}.
        </div>
      )}

      {/* § METADATA */}
      <section className="mb-12" aria-labelledby="meta-h">
        <header className="flex items-baseline gap-3 pb-3 border-b border-border mb-4">
          <span id="meta-h" className="coord-ink">§ METADATA</span>
        </header>

        <div className="space-y-5">
          <div>
            <Label className="coord-ink mb-1.5 block">NAME</Label>
            <Input value={name} disabled className="font-mono" />
            <p className="text-xs text-foreground-muted mt-1.5">
              Vault names are immutable. Create a new vault and migrate if you need a rename.
            </p>
          </div>

          <div>
            <Label htmlFor="vault-description" className="coord-ink mb-1.5 block">
              DESCRIPTION
            </Label>
            <Textarea
              id="vault-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              disabled={!canEdit || saving}
              placeholder="One sentence on what lives in this vault."
              rows={2}
              className="resize-y"
            />
          </div>

          <div>
            <Label className="coord-ink mb-1.5 block">PUBLIC ACCESS</Label>
            <div className="grid grid-cols-3 gap-px border border-border bg-border">
              {(["none", "reader", "writer"] as PublicAccess[]).map((v) => {
                const active = publicAccess === v;
                const Icon = v === "none" ? Lock : Globe;
                return (
                  <button
                    key={v}
                    type="button"
                    onClick={() => canEdit && !saving && setPublicAccess(v)}
                    aria-pressed={active}
                    disabled={!canEdit || saving}
                    className={`px-3 py-2 text-sm font-mono uppercase tracking-wider transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset disabled:opacity-50 disabled:cursor-not-allowed ${
                      active
                        ? "bg-foreground text-background"
                        : "bg-surface text-foreground hover:bg-surface-muted cursor-pointer"
                    }`}
                  >
                    <span className="inline-flex items-center justify-center gap-1.5">
                      <Icon className="h-3 w-3" aria-hidden />
                      {PUBLIC_LABELS[v]}
                    </span>
                  </button>
                );
              })}
            </div>
            <p className="text-xs text-foreground-muted mt-2 leading-relaxed">
              {PUBLIC_DESCRIPTIONS[publicAccess]}
            </p>
          </div>

          {error && (
            <div role="alert" className="border border-destructive p-2 text-xs text-destructive">
              {error}
            </div>
          )}

          {canEdit && (
            <div className="flex items-center gap-3">
              <Button
                variant="accent"
                onClick={handleSave}
                disabled={!dirty || saving}
              >
                <Save className="h-4 w-4" aria-hidden />
                {saving ? "Saving…" : "Save changes"}
              </Button>
              {savedAt && (
                <span className="coord-spark fade-in">SAVED</span>
              )}
              {dirty && !savedAt && !saving && (
                <span className="coord">UNSAVED CHANGES</span>
              )}
            </div>
          )}
        </div>
      </section>

      {/* § LIFECYCLE */}
      {canEdit && (
        <section aria-labelledby="lifecycle-h">
          <header className="flex items-baseline gap-3 pb-3 border-b border-border mb-4">
            <span id="lifecycle-h" className="coord-ink">§ LIFECYCLE</span>
          </header>

          <div className="space-y-5">
            <div className="border border-border p-4">
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
                  <Button
                    variant="outline"
                    onClick={() => setPendingUnarchive(true)}
                  >
                    <RotateCcw className="h-4 w-4" aria-hidden />
                    Unarchive
                  </Button>
                ) : (
                  <Button
                    variant="outline"
                    onClick={() => setPendingArchive(true)}
                  >
                    <Archive className="h-4 w-4" aria-hidden />
                    Archive
                  </Button>
                )}
              </div>
            </div>

            <div className="border border-border p-4">
              <h3 className="text-base font-semibold tracking-tight mb-1">
                Transfer ownership
              </h3>
              <p className="text-sm text-foreground-muted leading-relaxed max-w-prose mb-3">
                Reassign ownership to another vault member. You become an admin
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
        <section aria-labelledby="danger-h" className="mt-12">
          <header className="flex items-baseline gap-3 pb-3 border-b border-destructive mb-4">
            <span id="danger-h" className="coord-spark text-destructive">
              § DANGER ZONE
            </span>
          </header>

          <div className="border border-destructive p-4">
            <div className="flex items-baseline justify-between flex-wrap gap-y-3">
              <div className="min-w-0 pr-4">
                <h3 className="text-base font-semibold tracking-tight mb-1 text-destructive">
                  Delete vault permanently
                </h3>
                <p className="text-sm text-foreground-muted leading-relaxed max-w-prose">
                  Removes the vault and everything inside it: documents, tables,
                  files (including S3 objects), embeddings, relations, sessions,
                  memories, and the git repository. Agents lose access immediately.
                  This cannot be undone — prefer{" "}
                  <span className="font-mono">Archive</span> if you only need to
                  freeze the vault.
                </p>
              </div>
              <Button
                variant="destructive"
                onClick={() => setDeleteOpen(true)}
              >
                <Trash2 className="h-4 w-4" aria-hidden />
                Delete vault
              </Button>
            </div>
          </div>
        </section>
      )}

      {vaultHealth && (
        <section aria-labelledby="diag-h" className="mt-12">
          <header className="flex items-baseline gap-3 pb-3 border-b border-border mb-4">
            <span id="diag-h" className="coord-ink">§ DIAGNOSTICS</span>
            <span className="coord">indexing pipeline</span>
          </header>
          <div className="grid grid-cols-2 gap-px border border-border bg-border">
            <DiagCell title="INDEXING" stats={vaultHealth.vector_store?.backfill?.upsert} />
            <DiagCell title="METADATA" stats={vaultHealth.metadata_backfill} />
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

      <DeleteVaultDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        vault={name}
        onDeleted={() => navigate("/vault")}
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
