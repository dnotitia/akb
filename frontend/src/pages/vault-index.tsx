import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  FileText,
  Files,
  FolderTree,
  Library,
  PanelsTopLeft,
  Plus,
  ShieldCheck,
  Table2,
  type LucideIcon,
} from "lucide-react";
import { listVaults } from "@/lib/api";
import type { VaultSummary } from "@/hooks/use-vaults";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useOpenVaultCreateDialog } from "@/contexts/vault-create-dialog-context";

const WORKSPACE_STEPS: Array<{ Icon: LucideIcon; title: string; body: string }> = [
  {
    Icon: Library,
    title: "Vault scope",
    body: "Permissions, members, and search scope load with the vault.",
  },
  {
    Icon: FolderTree,
    title: "Collections",
    body: "Documents, tables, and files appear as one browsable tree.",
  },
  {
    Icon: PanelsTopLeft,
    title: "Workspace tools",
    body: "Overview, search, graph, publishing, and settings open in the canvas.",
  },
];

const KNOWLEDGE_SHAPES: Array<{ Icon: LucideIcon; title: string; body: string }> = [
  {
    Icon: FileText,
    title: "Documents",
    body: "Guides, notes, runbooks, and agent-ready context.",
  },
  {
    Icon: Table2,
    title: "Tables",
    body: "Structured knowledge that agents can query directly.",
  },
  {
    Icon: Files,
    title: "Files",
    body: "Reference material kept beside the knowledge it supports.",
  },
];

/** `/vault` is the neutral state of the master-detail workspace. The vault rail
 *  is the primary selector on desktop, while compact screens get direct links
 *  so the next action never depends on an icon-only navigation control. */
export default function VaultIndexPage() {
  const [vaults, setVaults] = useState<VaultSummary[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setVaults(null);
    setLoadError(false);
    listVaults()
      .then((data) => {
        if (!cancelled) setVaults((data.vaults as VaultSummary[]) || []);
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  if (loadError) {
    return (
      <div className="fade-up mx-auto flex min-h-[60vh] w-full max-w-3xl items-center justify-center">
        <Alert variant="destructive" title="Vaults could not be loaded" className="w-full">
          The workspace is still available, but the vault directory did not respond.
          <div className="mt-3">
            <Button variant="outline" size="sm" onClick={() => setReloadKey((key) => key + 1)}>
              Try again
            </Button>
          </div>
        </Alert>
      </div>
    );
  }

  if (vaults === null) return <VaultIndexLoading />;
  if (vaults.length === 0) return <FirstVaultState />;
  return <VaultSelectionState vaults={vaults} />;
}

function VaultIndexLoading() {
  return (
    <div
      className="fade-up mx-auto flex min-h-[60vh] w-full max-w-6xl items-center py-6"
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <div className="grid w-full overflow-hidden rounded-[var(--radius-xl)] border border-border bg-surface shadow-sm lg:grid-cols-[minmax(0,1.15fr)_minmax(20rem,0.85fr)]">
        <div className="p-6 sm:p-8 lg:p-10">
          <div className="h-11 w-11 animate-pulse rounded-[var(--radius-lg)] bg-surface-muted" aria-hidden />
          <div className="mt-8 h-10 w-3/4 max-w-lg animate-pulse rounded-[var(--radius-md)] bg-surface-muted" aria-hidden />
          <div className="mt-4 h-4 w-full max-w-xl animate-pulse rounded-[var(--radius-sm)] bg-surface-muted" aria-hidden />
          <div className="mt-2 h-4 w-2/3 max-w-md animate-pulse rounded-[var(--radius-sm)] bg-surface-muted" aria-hidden />
        </div>
        <div className="border-t border-border bg-surface-2 p-6 sm:p-8 lg:border-l lg:border-t-0 lg:p-10" aria-hidden>
          <div className="h-5 w-32 animate-pulse rounded-[var(--radius-sm)] bg-surface" />
          <div className="mt-8 space-y-4">
            <div className="h-16 animate-pulse rounded-[var(--radius-md)] bg-surface" />
            <div className="h-16 animate-pulse rounded-[var(--radius-md)] bg-surface" />
            <div className="h-16 animate-pulse rounded-[var(--radius-md)] bg-surface" />
          </div>
        </div>
        <span className="sr-only">Loading vault workspace</span>
      </div>
    </div>
  );
}

function VaultSelectionState({ vaults }: { vaults: VaultSummary[] }) {
  const openCreateVault = useOpenVaultCreateDialog();
  const owned = vaults.filter((vault) => vault.role === "owner").length;
  const shared = vaults.length - owned;

  return (
    <div className="fade-up mx-auto flex min-h-[60vh] w-full max-w-6xl items-center py-6">
      <section
        aria-labelledby="vault-selection-heading"
        className="grid w-full overflow-hidden rounded-[var(--radius-xl)] border border-border bg-surface shadow-sm lg:grid-cols-[minmax(0,1.15fr)_minmax(20rem,0.85fr)]"
      >
        <div className="p-6 sm:p-8 lg:p-10">
          <div className="flex items-center gap-3">
            <span
              className="inline-flex h-10 w-10 items-center justify-center rounded-[var(--radius-lg)] border border-border-strong bg-surface-selected text-surface-selected-foreground"
              aria-hidden
            >
              <Library className="h-4 w-4" />
            </span>
            <div>
              <p className="coord-ink">Vault navigator</p>
              <p className="mt-0.5 text-xs text-foreground-muted">Your knowledge spaces are ready</p>
            </div>
          </div>

          <h1
            id="vault-selection-heading"
            className="mt-8 max-w-2xl font-display text-3xl font-semibold tracking-tight text-foreground sm:text-4xl"
        >
          <span className="block">Open a vault.</span>{" "}
          <span className="block">Everything else follows.</span>
        </h1>
          <p className="mt-4 max-w-xl text-base leading-relaxed text-foreground-muted">
            Choose a vault to bring its collections, permissions, search, and knowledge graph into this workspace.
          </p>

          <div className="mt-7 flex flex-wrap items-center gap-4">
            <Button type="button" variant="outline" size="md" onClick={openCreateVault}>
              <Plus className="h-4 w-4" aria-hidden />
              New vault
            </Button>
            <p className="hidden items-center gap-2 text-sm text-foreground-muted lg:flex">
              <ArrowLeft className="h-4 w-4 text-primary" aria-hidden />
              Select from the navigator
            </p>
          </div>

          <dl
            aria-label={`${vaults.length.toLocaleString()} ${vaults.length === 1 ? "vault available" : "vaults available"}`}
            className="mt-10 grid max-w-xl grid-cols-3 border-y border-border"
          >
            <div className="py-3 pr-3">
              <dt className="text-xs text-foreground-muted">Available</dt>
              <dd className="mt-1 tabular-nums text-base font-semibold text-foreground">
                {vaults.length.toLocaleString()} {vaults.length === 1 ? "vault" : "vaults"}
              </dd>
            </div>
            <div className="border-l border-border px-3 py-3">
              <dt className="text-xs text-foreground-muted">Owned</dt>
              <dd className="mt-1 tabular-nums text-base font-semibold text-foreground">{owned.toLocaleString()}</dd>
            </div>
            <div className="border-l border-border py-3 pl-3">
              <dt className="text-xs text-foreground-muted">Shared</dt>
              <dd className="mt-1 tabular-nums text-base font-semibold text-foreground">{shared.toLocaleString()}</dd>
            </div>
          </dl>
        </div>

        <CompactVaultLinks vaults={vaults} />
        <WorkspaceRoute />
      </section>
    </div>
  );
}

function CompactVaultLinks({ vaults }: { vaults: VaultSummary[] }) {
  return (
    <section className="border-t border-border p-4 sm:p-6 lg:hidden" aria-labelledby="compact-vault-links-heading">
      <div className="flex items-center justify-between gap-3">
        <h2 id="compact-vault-links-heading" className="text-sm font-semibold text-foreground">
          Open directly
        </h2>
        <span className="text-xs text-foreground-muted">{vaults.length.toLocaleString()} available</span>
      </div>
      <div className="mt-3 divide-y divide-border overflow-hidden rounded-[var(--radius-lg)] border border-border bg-background">
        {vaults.slice(0, 4).map((vault) => (
          <Link
            key={vault.id}
            to={`/vault/${vault.name}`}
            className="flex min-h-11 items-center justify-between px-3 text-sm font-medium text-foreground transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
          >
            <span className="min-w-0 truncate">{vault.name}</span>
            <span className="ml-3 flex shrink-0 items-center gap-2 text-xs text-foreground-muted">
              {vault.role || "member"}
              <ArrowRight className="h-3.5 w-3.5" aria-hidden />
            </span>
          </Link>
        ))}
      </div>
      {vaults.length > 4 && (
        <p className="mt-2 text-xs text-foreground-muted">
          Open the navigator above to see {vaults.length - 4} more.
        </p>
      )}
    </section>
  );
}

function WorkspaceRoute() {
  return (
    <aside
      className="border-t border-border bg-surface-2 p-6 sm:p-8 lg:border-l lg:border-t-0 lg:p-10"
      aria-labelledby="workspace-route-heading"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="coord">What opens next</p>
          <h2 id="workspace-route-heading" className="mt-1 text-base font-semibold text-foreground">
            Workspace route
          </h2>
        </div>
        <span className="rounded-[var(--radius-full)] border border-border bg-surface px-2.5 py-1 text-xs text-foreground-muted">
          Awaiting selection
        </span>
      </div>

      <ol className="mt-8">
        {WORKSPACE_STEPS.map(({ Icon, title, body }, index) => (
          <li key={title} className="relative flex gap-4 pb-6 last:pb-0">
            {index < WORKSPACE_STEPS.length - 1 && (
              <span className="absolute bottom-0 left-5 top-10 w-px bg-border-strong" aria-hidden />
            )}
            <span className="relative z-10 inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-border-strong bg-surface text-primary shadow-xs">
              <Icon className="h-4 w-4" aria-hidden />
            </span>
            <div className="min-w-0 pt-0.5">
              <h3 className="text-sm font-semibold text-foreground">{title}</h3>
              <p className="mt-1 text-sm leading-relaxed text-foreground-muted">{body}</p>
            </div>
          </li>
        ))}
      </ol>

      <div className="mt-8 flex items-start gap-3 border-t border-border pt-5">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
        <p className="text-xs leading-relaxed text-foreground-muted">
          Vault boundaries stay intact as the workspace changes around your selection.
        </p>
      </div>
    </aside>
  );
}

function FirstVaultState() {
  const openCreateVault = useOpenVaultCreateDialog();

  return (
    <div className="fade-up mx-auto flex min-h-[60vh] w-full max-w-6xl items-center py-6">
      <section
        aria-labelledby="first-vault-heading"
        className="grid w-full overflow-hidden rounded-[var(--radius-xl)] border border-border bg-surface shadow-sm lg:grid-cols-[minmax(0,1.15fr)_minmax(20rem,0.85fr)]"
      >
        <div className="p-6 sm:p-8 lg:p-10">
          <span
            className="inline-flex h-10 w-10 items-center justify-center rounded-[var(--radius-lg)] border border-border-strong bg-surface-selected text-surface-selected-foreground"
            aria-hidden
          >
            <Library className="h-4 w-4" />
          </span>
          <p className="coord-ink mt-6">Start your knowledge workspace</p>
          <h1
            id="first-vault-heading"
            className="mt-2 max-w-2xl font-display text-3xl font-semibold tracking-tight text-foreground sm:text-4xl"
          >
            Create your first vault.
          </h1>
          <p className="mt-4 max-w-xl text-base leading-relaxed text-foreground-muted">
            Give your team and connected agents one governed place for documents, structured data, files, and the relationships between them.
          </p>
          <Button type="button" variant="accent" size="md" className="mt-7" onClick={openCreateVault}>
            <Plus className="h-4 w-4" aria-hidden />
            Create first vault
          </Button>

          <div className="mt-10 border-y border-border py-4">
            <p className="text-sm font-medium text-foreground">Start with a name. Add structure when you need it.</p>
            <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
              Collections, access rules, and agent connections can evolve with the vault.
            </p>
          </div>
        </div>

        <KnowledgeShapeRoute />
      </section>
    </div>
  );
}

function KnowledgeShapeRoute() {
  return (
    <aside
      className="border-t border-border bg-surface-2 p-6 sm:p-8 lg:border-l lg:border-t-0 lg:p-10"
      aria-labelledby="knowledge-shapes-heading"
    >
      <p className="coord">Designed for agent context</p>
      <h2 id="knowledge-shapes-heading" className="mt-1 text-base font-semibold text-foreground">
        Every knowledge shape, together
      </h2>
      <div className="mt-8 divide-y divide-border">
        {KNOWLEDGE_SHAPES.map(({ Icon, title, body }) => (
          <div key={title} className="flex gap-4 py-5 first:pt-0 last:pb-0">
            <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-border bg-surface text-primary">
              <Icon className="h-4 w-4" aria-hidden />
            </span>
            <div className="min-w-0">
              <h3 className="text-sm font-semibold text-foreground">{title}</h3>
              <p className="mt-1 text-sm leading-relaxed text-foreground-muted">{body}</p>
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}
