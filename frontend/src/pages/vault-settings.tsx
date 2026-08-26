import { useEffect, useRef, useState, type RefObject } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  Archive,
  ArrowLeft,
  BookOpen,
  Box,
  CheckCircle2,
  CircleDashed,
  Globe,
  Lock,
  RotateCcw,
  Save,
  Settings2,
  ShieldCheck,
  Trash2,
  Unlock,
  Users,
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
import { SkillSection } from "@/components/skill/skill-section";
import { VAULT_SKILL_PATH } from "@/lib/skill";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { CopyButton } from "@/components/ui/copy-button";
import { Label } from "@/components/ui/label";
import { PageShell } from "@/components/ui/page-shell";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { VaultContextBadge } from "@/components/ui/vault-context-badge";
import { Segmented } from "@/components/ui/segmented";
import { SettingsSectionHeader } from "@/components/ui/settings-section-header";
import { TonalIcon } from "@/components/ui/tonal-icon";
import { Textarea } from "@/components/ui/textarea";
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
  role_source?: "member" | "public";
  status?: string;
  is_archived?: boolean;
  is_external_git?: boolean;
  public_access?: "none" | "reader" | "writer";
  owner?: string;
  owner_display_name?: string;
  created_at?: string;
  last_activity?: string;
  member_count?: number;
  collection_count?: number;
  document_count?: number;
  table_count?: number;
  file_count?: number;
  edge_count?: number;
  tables?: TableMeta[];
}

type PublicAccess = "none" | "reader" | "writer";
type SaveScope = "general" | "access";

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
    "Any signed-in person with the link can read this vault. Writes still require an invitation.",
  writer:
    "Any signed-in person with the link can create, edit, and delete content. Use this only for deliberately open collaboration.",
};
const PUBLIC_ORDER: PublicAccess[] = ["none", "reader", "writer"];

export default function VaultSettingsPage() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { refetchVaults } = useVaultRefresh();
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const [loadError, setLoadError] = useState("");
  const [description, setDescription] = useState("");
  const [publicAccess, setPublicAccess] = useState<PublicAccess>("none");
  const [savingScope, setSavingScope] = useState<SaveScope | null>(null);
  const [savedScope, setSavedScope] = useState<SaveScope | null>(null);
  const [saveError, setSaveError] = useState("");
  const [saveErrorScope, setSaveErrorScope] = useState<SaveScope | null>(null);
  const [pendingArchive, setPendingArchive] = useState(false);
  const [pendingUnarchive, setPendingUnarchive] = useState(false);
  const [pendingPublicWrite, setPendingPublicWrite] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const generalStatusRef = useRef<HTMLSpanElement>(null);
  const accessStatusRef = useRef<HTMLSpanElement>(null);
  const settingsMainRef = useRef<HTMLElement>(null);
  const vaultHealth = useVaultHealth(name);

  const skillQuery = useQuery({
    queryKey: ["document", name, VAULT_SKILL_PATH],
    queryFn: () => getDocument(name!, VAULT_SKILL_PATH),
    retry: false,
    enabled: !!name && !!info && !info.is_external_git,
  });
  const skillDoc = skillQuery.isError ? null : skillQuery.data;

  function loadInfo(vault: string) {
    setLoadError("");
    getVaultInfo(vault)
      .then((data) => {
        setInfo(data);
        setDescription(data.description || "");
        setPublicAccess((data.public_access as PublicAccess) || "none");
      })
      .catch((error) =>
        setLoadError(error?.message || "Couldn't load this vault."),
      );
  }

  useEffect(() => {
    if (!name) return;
    setInfo(null);
    setLoadError("");
    setDescription("");
    setPublicAccess("none");
    setSaveError("");
    setSaveErrorScope(null);
    setSavedScope(null);
    loadInfo(name);
  }, [name]);

  useEffect(() => {
    const target = location.hash.slice(1);
    if (!["general", "access", "skill", "danger"].includes(target)) return;
    const reduce = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    requestAnimationFrame(() => {
      const targetElement = document.getElementById(target);
      if (!targetElement) return;
      const mainPane = settingsMainRef.current;
      if (mainPane && window.matchMedia("(min-width: 1280px)").matches) {
        const paneTop = mainPane.getBoundingClientRect().top;
        const targetTop = targetElement.getBoundingClientRect().top;
        mainPane.scrollTo({
          top: Math.max(0, mainPane.scrollTop + targetTop - paneTop - 24),
          behavior: reduce ? "auto" : "smooth",
        });
        return;
      }
      targetElement.scrollIntoView({
        behavior: reduce ? "auto" : "smooth",
        block: "start",
      });
    });
  }, [info, location.hash, location.key, skillQuery.isLoading]);

  useEffect(() => {
    if (!name) return;
    const previous = document.title;
    document.title = `${name} · Settings · AKB`;
    return () => {
      document.title = previous;
    };
  }, [name]);

  const canEdit = info?.role === "owner";
  const canManageSkill = info?.role === "owner" && !info?.is_archived;
  const savedPublic = (info?.public_access as PublicAccess) || "none";
  const descriptionDirty = Boolean(
    info && description !== (info.description || ""),
  );
  const accessDirty = Boolean(info && publicAccess !== savedPublic);
  const dirty = descriptionDirty || accessDirty;
  const saving = savingScope !== null;

  useEffect(() => {
    if (!dirty || saving) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty, saving]);

  function requestSave(scope: SaveScope) {
    if (!info) return;
    const enablingPublicWrite =
      scope === "access" &&
      publicAccess === "writer" &&
      savedPublic !== "writer";
    if (enablingPublicWrite) {
      setPendingPublicWrite(true);
      return;
    }
    void doSave(scope);
  }

  async function doSave(scope: SaveScope) {
    if (!name || !info) return;
    setSavingScope(scope);
    setSaveError("");
    setSaveErrorScope(null);
    try {
      const patch =
        scope === "general" ? { description } : { public_access: publicAccess };
      await updateVault(name, patch);
      setInfo({ ...info, ...patch });
      setSavedScope(scope);
      refetchVaults();
      requestAnimationFrame(() => {
        (scope === "general"
          ? generalStatusRef
          : accessStatusRef
        ).current?.focus();
      });
    } catch (error: unknown) {
      setSaveError(error instanceof Error ? error.message : "Save failed");
      setSaveErrorScope(scope);
    } finally {
      setSavingScope(null);
    }
  }

  function handleDiscard(scope: SaveScope) {
    if (!info) return;
    if (scope === "general") setDescription(info.description || "");
    else setPublicAccess(savedPublic);
    setSaveError("");
    setSaveErrorScope(null);
    setSavedScope(null);
  }

  async function confirmArchive() {
    if (!name) return;
    await archiveVault(name);
    setInfo(await getVaultInfo(name));
    refetchVaults();
  }

  async function confirmUnarchive() {
    if (!name) return;
    await unarchiveVault(name);
    setInfo(await getVaultInfo(name));
    refetchVaults();
  }

  if (!name) return null;

  const loading = info === null && !loadError;
  const tables = info?.tables ?? [];
  const deleteScale = info
    ? (
        [
          [info.document_count, "document"],
          [info.table_count, "table"],
          [info.file_count, "file"],
        ] as Array<[number | undefined, string]>
      )
        .filter(([count]) => (count ?? 0) > 0)
        .map(
          ([count, word]) =>
            `${count!.toLocaleString()} ${word}${count === 1 ? "" : "s"}`,
        )
        .join(", ")
    : "";

  return (
    <PageShell
      header={null}
      contentWidth="full"
      className="min-h-full xl:h-full xl:min-h-0"
      contentClassName="min-h-full xl:flex xl:h-full xl:min-h-0 xl:flex-col"
    >
      <h1 className="sr-only">Settings</h1>

      {loadError && (
        <Alert variant="destructive" className="mb-5">
          {loadError}
          <div className="mt-3">
            <Button variant="outline" size="sm" onClick={() => loadInfo(name)}>
              Try again
            </Button>
          </div>
        </Alert>
      )}

      {loading && <SettingsLoadingState />}

      {info && (
        <div
          data-testid="settings-workspace-shell"
          className="min-h-full w-full xl:flex xl:min-h-0 xl:flex-1 xl:flex-col"
        >
          <div
            data-testid="settings-workspace-frame"
            className="flex min-h-full w-full flex-col xl:min-h-0 xl:flex-1 xl:overflow-hidden"
          >
            {!canEdit && (
              <Alert
                variant="info"
                className="shrink-0 rounded-none border-x-0 border-t-0 border-b px-4 py-2 sm:px-5 lg:px-6"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span>
                    You can review these settings, but only the vault owner can
                    change them.
                  </span>
                  {info.role_source === "public" && (
                    <Badge
                      variant="info-outline"
                      title="This role comes from public access."
                    >
                      <Globe className="h-3 w-3" aria-hidden />
                      Access via public policy
                    </Badge>
                  )}
                </div>
              </Alert>
            )}

            <div className="grid min-h-full w-full items-stretch bg-background lg:grid-cols-[10.5rem_minmax(0,1fr)] xl:min-h-0 xl:flex-1 xl:grid-cols-[10.5rem_minmax(0,1fr)_17rem]">
              <nav
                aria-label="Vault settings sections"
                className="sticky top-0 z-[var(--z-sticky)] min-w-0 overflow-hidden border-b border-border bg-surface lg:static lg:col-start-1 lg:row-start-1 lg:overflow-visible lg:border-r lg:border-b-0 xl:min-h-0 xl:overflow-y-auto"
              >
                <div className="flex min-w-0 gap-1 overflow-x-auto bg-surface p-1 lg:sticky lg:top-0 lg:flex-col lg:overflow-visible lg:p-2 xl:static">
                  {[
                    { href: "#general", label: "General", Icon: Settings2 },
                    { href: "#access", label: "Access", Icon: ShieldCheck },
                    { href: "#skill", label: "Vault guide", Icon: BookOpen },
                    ...(canEdit
                      ? [
                          {
                            href: "#danger",
                            label: "Danger zone",
                            Icon: AlertTriangle,
                          },
                        ]
                      : []),
                  ].map(({ href, label, Icon }) => {
                    const active = (location.hash || "#general") === href;
                    return (
                      <a
                        key={href}
                        href={href}
                        onClick={(event) => {
                          event.preventDefault();
                          navigate({
                            pathname: location.pathname,
                            search: location.search,
                            hash: href,
                          });
                        }}
                        aria-current={active ? "location" : undefined}
                        className={`inline-flex h-8 shrink-0 items-center gap-2 rounded-[var(--radius-sm)] px-2.5 text-xs font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                          active
                            ? "bg-surface-selected text-surface-selected-foreground"
                            : "text-foreground-muted hover:bg-surface-hover hover:text-foreground"
                        }`}
                      >
                        <Icon className="h-3.5 w-3.5" aria-hidden />
                        {label}
                      </a>
                    );
                  })}
                </div>
              </nav>

              <main
                ref={settingsMainRef}
                className="rail-scroll min-w-0 space-y-6 p-4 sm:p-5 lg:col-start-2 lg:row-start-1 lg:p-6 xl:min-h-0 xl:overflow-y-auto xl:overscroll-contain xl:scroll-py-6 xl:px-7"
              >
                <section
                  id="general"
                  role="region"
                  aria-labelledby="general-settings-heading"
                  className="scroll-mt-6"
                >
                  <SettingsSectionHeader
                    id="general-settings-heading"
                    icon={Settings2}
                    title="General"
                    description="The identity people see across the workspace."
                    tone="knowledge"
                  />
                  <Panel variant="workspace" className="border-border-strong">
                    <div className="grid gap-4 p-4 2xl:grid-cols-[minmax(16rem,0.8fr)_minmax(22rem,1.2fr)]">
                      <div>
                        <Label className="mb-1.5 block">Vault address</Label>
                        <div className="flex min-h-10 items-center justify-between gap-3 rounded-[var(--radius-md)] border border-border bg-surface-2 px-3">
                          <span className="truncate font-mono text-sm text-foreground">
                            akb://{name}
                          </span>
                          <CopyButton
                            value={`akb://${name}`}
                            label="Copy vault address"
                          />
                        </div>
                        <p className="mt-1.5 text-xs leading-relaxed text-foreground-muted">
                          Vault names are permanent because every document URI
                          depends on this address.
                        </p>
                      </div>

                      <div>
                        <Label
                          htmlFor="vault-description"
                          className="mb-1.5 block"
                        >
                          Description
                        </Label>
                        <Textarea
                          id="vault-description"
                          value={description}
                          onChange={(event) => {
                            setDescription(event.target.value);
                            setSavedScope(null);
                          }}
                          readOnly={!canEdit}
                          disabled={saving}
                          placeholder="Explain what belongs in this vault and who it serves."
                          rows={3}
                          className="resize-y"
                        />
                        <p className="mt-1.5 text-xs leading-relaxed text-foreground-muted">
                          This description appears in Vault Overview and helps
                          people choose the right knowledge space.
                        </p>
                      </div>

                      {saveErrorScope === "general" && (
                        <Alert variant="destructive">{saveError}</Alert>
                      )}
                    </div>
                    {canEdit && (
                      <SettingsActionBar
                        dirty={descriptionDirty}
                        saving={savingScope === "general"}
                        saved={savedScope === "general" && !descriptionDirty}
                        statusRef={generalStatusRef}
                        onSave={() => requestSave("general")}
                        onDiscard={() => handleDiscard("general")}
                      />
                    )}
                  </Panel>
                </section>

                <section
                  id="access"
                  role="region"
                  aria-labelledby="access-settings-heading"
                  className="scroll-mt-6"
                >
                  <SettingsSectionHeader
                    id="access-settings-heading"
                    icon={ShieldCheck}
                    title="Access"
                    description="Set the vault-wide default without changing individual member roles."
                    tone={publicAccess === "none" ? "info" : "success"}
                  />
                  <Panel variant="workspace" className="border-border-strong">
                    <div className="space-y-4 p-4">
                      <div>
                        <Label id="public-access-label" className="mb-2 block">
                          Public access
                        </Label>
                        <Segmented
                          aria-labelledby="public-access-label"
                          value={publicAccess}
                          onChange={(value) => {
                            setPublicAccess(value as PublicAccess);
                            setSavedScope(null);
                          }}
                          disabled={!canEdit || saving}
                          className="grid-cols-1 sm:grid-cols-3"
                          options={PUBLIC_ORDER.map((value) => {
                            const Icon = PUBLIC_ICONS[value];
                            return {
                              value,
                              label: PUBLIC_LABELS[value],
                              icon: (
                                <Icon className="h-3.5 w-3.5" aria-hidden />
                              ),
                              danger: value === "writer",
                            };
                          })}
                        />
                        <p
                          className={`mt-3 flex items-start gap-2 text-xs leading-relaxed ${
                            publicAccess === "writer"
                              ? "text-warning-soft-foreground"
                              : "text-foreground-muted"
                          }`}
                        >
                          {publicAccess === "writer" ? (
                            <AlertTriangle
                              className="mt-0.5 h-3.5 w-3.5 shrink-0"
                              aria-hidden
                            />
                          ) : publicAccess === "none" ? (
                            <Lock
                              className="mt-0.5 h-3.5 w-3.5 shrink-0"
                              aria-hidden
                            />
                          ) : (
                            <Globe
                              className="mt-0.5 h-3.5 w-3.5 shrink-0"
                              aria-hidden
                            />
                          )}
                          <span>{PUBLIC_DESCRIPTIONS[publicAccess]}</span>
                        </p>
                      </div>

                      {saveErrorScope === "access" && (
                        <Alert variant="destructive">{saveError}</Alert>
                      )}
                    </div>
                    {canEdit && (
                      <SettingsActionBar
                        dirty={accessDirty}
                        saving={savingScope === "access"}
                        saved={savedScope === "access" && !accessDirty}
                        statusRef={accessStatusRef}
                        onSave={() => requestSave("access")}
                        onDiscard={() => handleDiscard("access")}
                      />
                    )}
                    <div className="flex flex-col gap-3 border-t border-border bg-surface-2 p-4 sm:flex-row sm:items-center sm:justify-between">
                      <div className="flex min-w-0 items-start gap-3">
                        <TonalIcon tone="people">
                          <Users className="h-4 w-4" aria-hidden />
                        </TonalIcon>
                        <div>
                          <h3 className="text-sm font-semibold text-foreground">
                            Members and ownership
                          </h3>
                          <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
                            Invite people, adjust roles, or transfer ownership
                            from the Members page.
                          </p>
                        </div>
                      </div>
                      <Button
                        asChild
                        variant="outline"
                        size="sm"
                        className="shrink-0"
                      >
                        <Link to={`/vault/${name}/members`}>Open members</Link>
                      </Button>
                    </div>
                  </Panel>
                </section>

                <SkillSection
                  vault={name}
                  doc={skillDoc}
                  loading={skillQuery.isLoading || (!info && !loadError)}
                  isMirror={info.is_external_git}
                  canManage={canManageSkill}
                />

                {canEdit && (
                  <Panel
                    variant="workspace"
                    id="danger"
                    role="region"
                    aria-labelledby="danger-settings-heading"
                    className="scroll-mt-4 border-destructive"
                  >
                    <div className="border-b border-destructive bg-destructive-soft px-3 py-2.5">
                      <div className="flex items-start gap-3">
                        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-sm)] border border-destructive text-destructive">
                          <AlertTriangle className="h-4 w-4" aria-hidden />
                        </span>
                        <div>
                          <h2
                            id="danger-settings-heading"
                            className="text-sm font-semibold text-destructive"
                          >
                            Danger zone
                          </h2>
                          <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
                            These actions change availability for every member
                            and connected agent.
                          </p>
                        </div>
                      </div>
                    </div>

                    <div className="divide-y divide-border">
                      <div className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
                        <div className="min-w-0">
                          <h3 className="flex items-center gap-2 text-sm font-semibold text-foreground">
                            {info.is_archived ? (
                              <Archive
                                className="h-4 w-4 text-foreground-muted"
                                aria-hidden
                              />
                            ) : (
                              <CheckCircle2
                                className="h-4 w-4 text-success"
                                aria-hidden
                              />
                            )}
                            {info.is_archived
                              ? "Vault archived"
                              : "Archive vault"}
                          </h3>
                          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-foreground-muted">
                            {info.is_archived
                              ? "The vault is read-only. Members and agents can still retrieve its knowledge."
                              : "Freeze all writes while keeping documents searchable. Archiving is reversible."}
                          </p>
                        </div>
                        {info.is_archived ? (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setPendingUnarchive(true)}
                          >
                            <RotateCcw className="h-4 w-4" aria-hidden />
                            Unarchive
                          </Button>
                        ) : (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setPendingArchive(true)}
                          >
                            <Archive className="h-4 w-4" aria-hidden />
                            Archive
                          </Button>
                        )}
                      </div>

                      <div className="flex flex-col gap-3 bg-destructive-soft p-4 sm:flex-row sm:items-center sm:justify-between">
                        <div className="min-w-0">
                          <h3 className="flex items-center gap-2 text-sm font-semibold text-destructive">
                            <Trash2 className="h-4 w-4" aria-hidden />
                            Delete vault permanently
                          </h3>
                          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-foreground-muted">
                            Removes everything inside
                            {deleteScale ? ` — ${deleteScale}` : ""}, plus git
                            history, relations, embeddings, sessions, and stored
                            files. This cannot be undone.
                          </p>
                          {tables.length > 0 && (
                            <p className="mt-2 max-w-2xl text-xs leading-relaxed text-foreground-muted">
                              Tables removed:{" "}
                              {tables
                                .slice(0, 4)
                                .map((table) => table.name)
                                .join(", ")}
                              {tables.length > 4
                                ? `, and ${tables.length - 4} more`
                                : ""}
                              .
                            </p>
                          )}
                        </div>
                        <Button
                          variant="destructive"
                          size="sm"
                          onClick={() => setDeleteOpen(true)}
                        >
                          <Trash2 className="h-4 w-4" aria-hidden />
                          Delete vault
                        </Button>
                      </div>
                    </div>
                  </Panel>
                )}
              </main>

              <aside
                aria-label="Vault settings context"
                className="rail-scroll min-w-0 border-t border-border bg-surface-2 p-4 sm:p-5 lg:col-start-2 lg:row-start-2 xl:col-start-3 xl:row-start-1 xl:min-h-0 xl:overflow-y-auto xl:overscroll-contain xl:border-t-0 xl:border-l xl:bg-surface xl:p-0"
              >
                <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-1 xl:gap-0">
                  <Panel
                    variant="workspace"
                    className="xl:rounded-none xl:border-x-0 xl:border-t-0 xl:shadow-none"
                  >
                    <div
                      data-slot="vault-context-header"
                      className="border-b border-border-strong bg-surface-2/55 p-3"
                    >
                      <div className="flex items-start gap-3">
                        <TonalIcon tone="knowledge">
                          <Box className="h-4 w-4" aria-hidden />
                        </TonalIcon>
                        <div className="min-w-0">
                          <p className="text-xs text-foreground-muted">
                            Current vault
                          </p>
                          <h2 className="mt-0.5 truncate font-display text-base font-semibold text-foreground">
                            {name}
                          </h2>
                        </div>
                      </div>
                    </div>

                    <div className="p-3">
                      <VaultContextBadge name={name} address copyable />

                      <div className="mt-3 flex flex-wrap gap-2">
                        {info.role && <RoleBadge role={info.role} />}
                        <VaultStateBadge
                          archived={info.is_archived}
                          externalGit={info.is_external_git}
                          publicAccess={info.public_access}
                        />
                      </div>
                    </div>
                    <Link
                      to={`/vault/${name}`}
                      className="flex min-h-10 items-center justify-between gap-3 border-t border-border px-3 text-xs font-medium text-link transition-token hover:bg-surface-hover hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                    >
                      Open Vault Overview
                      <ArrowLeft className="h-4 w-4 rotate-180" aria-hidden />
                    </Link>
                  </Panel>

                  <Panel
                    variant="workspace"
                    className="xl:rounded-none xl:border-x-0 xl:border-t-0 xl:shadow-none"
                  >
                    <PanelHeader
                      variant="workspace"
                      label="About"
                      className="border-border-strong bg-surface-2/55"
                    />
                    <dl className="divide-y divide-border px-4 text-sm">
                      <ContextRow
                        label="Owner"
                        value={
                          info.owner_display_name || info.owner || "Unknown"
                        }
                      />
                      <ContextRow
                        label="Created"
                        value={
                          info.created_at ? timeAgo(info.created_at) : "Unknown"
                        }
                      />
                      <ContextRow
                        label="Last active"
                        value={
                          info.last_activity
                            ? timeAgo(info.last_activity)
                            : "No activity"
                        }
                      />
                      <ContextRow
                        label="Members"
                        value={(info.member_count ?? 0).toLocaleString()}
                      />
                    </dl>
                    <div className="grid grid-cols-2 border-t border-border bg-surface-2">
                      <SnapshotCell
                        label="Collections"
                        value={info.collection_count ?? 0}
                      />
                      <SnapshotCell
                        label="Documents"
                        value={info.document_count ?? 0}
                        bordered
                      />
                      <SnapshotCell
                        label="Tables"
                        value={info.table_count ?? 0}
                        top
                      />
                      <SnapshotCell
                        label="Files"
                        value={info.file_count ?? 0}
                        bordered
                        top
                      />
                    </div>
                    <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-3 text-xs">
                      <span className="text-foreground-muted">Graph links</span>
                      <span className="tabular-nums text-foreground">
                        {(info.edge_count ?? 0).toLocaleString()}
                      </span>
                    </div>
                  </Panel>

                  {vaultHealth && (
                    <Panel
                      variant="workspace"
                      className="xl:rounded-none xl:border-x-0 xl:border-t-0 xl:shadow-none"
                    >
                      <PanelHeader
                        variant="workspace"
                        label="Operations"
                        className="border-border-strong bg-surface-2/55"
                      />
                      <div className="divide-y divide-border">
                        <PipelineStatus
                          label="Indexing"
                          stats={vaultHealth.vector_store?.backfill?.upsert}
                        />
                        <PipelineStatus
                          label="Metadata"
                          stats={vaultHealth.metadata_backfill}
                        />
                      </div>
                      <p className="border-t border-border px-4 py-3 text-xs leading-relaxed text-foreground-muted">
                        New content is processed asynchronously after each
                        write.
                      </p>
                    </Panel>
                  )}
                </div>
              </aside>
            </div>
          </div>
        </div>
      )}

      <span className="sr-only" role="status" aria-live="polite">
        {loading
          ? "Loading vault settings"
          : loadError
            ? "Could not load settings"
            : ""}
      </span>

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
            <AlertTriangle
              className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
              aria-hidden
            />
            <span>
              Any signed-in person with the link will be able to create, edit,
              and delete content in this vault. You can lower access again
              later.
            </span>
          </span>
        }
        confirmLabel="Make world-writable"
        onConfirm={() => doSave("access")}
      />

      <DeleteVaultDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        vault={name}
        onDeleted={() => {
          refetchVaults();
          navigate("/vault");
        }}
      />
    </PageShell>
  );
}

function SettingsActionBar({
  dirty,
  saving,
  saved,
  statusRef,
  onSave,
  onDiscard,
}: {
  dirty: boolean;
  saving: boolean;
  saved: boolean;
  statusRef: RefObject<HTMLSpanElement | null>;
  onSave: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-border bg-surface-2 px-4 py-2.5">
      <Button
        type="button"
        size="sm"
        onClick={onSave}
        loading={saving}
        disabled={!dirty}
      >
        {!saving && <Save className="h-3.5 w-3.5" aria-hidden />}
        {saving ? "Saving…" : "Save changes"}
      </Button>
      {dirty && !saving && (
        <Button type="button" variant="outline" size="sm" onClick={onDiscard}>
          Discard
        </Button>
      )}
      <span
        ref={statusRef}
        tabIndex={-1}
        role="status"
        aria-live="polite"
        className="outline-none"
      >
        {dirty ? (
          <span className="text-xs text-foreground-muted">Unsaved changes</span>
        ) : saved ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-success">
            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
            Saved
          </span>
        ) : null}
      </span>
    </div>
  );
}

function ContextRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 py-2.5 text-xs">
      <dt className="text-foreground-muted">{label}</dt>
      <dd className="truncate text-right font-medium text-foreground">
        {value}
      </dd>
    </div>
  );
}

function SnapshotCell({
  label,
  value,
  bordered = false,
  top = false,
}: {
  label: string;
  value: number;
  bordered?: boolean;
  top?: boolean;
}) {
  return (
    <div
      className={`${bordered ? "border-l border-border" : ""} ${top ? "border-t border-border" : ""} px-3 py-2.5`}
    >
      <p className="text-xs text-foreground-muted">{label}</p>
      <p className="mt-1 text-sm font-semibold tabular-nums text-foreground">
        {value.toLocaleString()}
      </p>
    </div>
  );
}

interface DiagStats {
  pending?: number;
  retrying?: number;
  abandoned?: number;
}

function diagVerdict(stats?: DiagStats): {
  label: string;
  className: string;
  Icon: LucideIcon;
} {
  const abandoned = stats?.abandoned ?? 0;
  const inFlight = (stats?.pending ?? 0) + (stats?.retrying ?? 0);
  if (abandoned > 0) {
    return {
      label: `${abandoned.toLocaleString()} need attention`,
      className: "text-destructive",
      Icon: AlertTriangle,
    };
  }
  if (inFlight > 0) {
    return {
      label: `${inFlight.toLocaleString()} in progress`,
      className: "text-warning",
      Icon: CircleDashed,
    };
  }
  return { label: "Caught up", className: "text-success", Icon: CheckCircle2 };
}

function PipelineStatus({
  label,
  stats,
}: {
  label: string;
  stats?: DiagStats;
}) {
  const verdict = diagVerdict(stats);
  const Icon = verdict.Icon;
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-3 text-sm">
      <span className="flex items-center gap-2 text-foreground">
        {label === "Indexing" ? (
          <Activity className="h-4 w-4 text-foreground-muted" aria-hidden />
        ) : (
          <BookOpen className="h-4 w-4 text-foreground-muted" aria-hidden />
        )}
        {label}
      </span>
      <span
        className={`inline-flex items-center gap-1.5 text-xs ${verdict.className}`}
      >
        <Icon className="h-3.5 w-3.5" aria-hidden />
        {verdict.label}
      </span>
    </div>
  );
}

function SettingsLoadingState() {
  return (
    <div
      className="grid gap-5 lg:grid-cols-[9.5rem_minmax(0,1fr)]"
      role="status"
      aria-label="Loading vault settings"
    >
      <div
        className="h-44 animate-pulse rounded-[var(--radius-md)] border border-border bg-surface"
        aria-hidden
      />
      <div className="space-y-4">
        {[0, 1, 2].map((index) => (
          <div
            key={index}
            className="h-48 animate-pulse rounded-[var(--radius-md)] border border-border bg-surface"
            aria-hidden
          />
        ))}
      </div>
    </div>
  );
}
