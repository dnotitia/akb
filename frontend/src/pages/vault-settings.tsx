import { useEffect, useRef, useState, type RefObject } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle,
  Archive,
  Box,
  CheckCircle2,
  ChevronRight,
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
import { Panel } from "@/components/ui/panel";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Segmented } from "@/components/ui/segmented";
import { WorkspaceSectionHeader } from "@/components/ui/workspace-section-header";
import { TonalIcon } from "@/components/ui/tonal-icon";
import { Textarea } from "@/components/ui/textarea";
import { DeleteVaultDialog } from "@/components/delete-vault-dialog";
import { RoleBadge, VaultStateBadge } from "@/components/status-badge";
import { useOptionalSearchStatus } from "@/hooks/use-search-status";
import { SearchStatusStages } from "@/components/search-status-stages";
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
type SettingsSection = SaveScope | "skill" | "danger";
const SETTINGS_SECTIONS: Array<{ value: SettingsSection; label: string }> = [
  { value: "general", label: "General" },
  { value: "access", label: "Access" },
  { value: "skill", label: "Vault guide" },
  { value: "danger", label: "Advanced" },
];

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
  return <VaultSettingsWorkspace key={name} name={name} />;
}

function VaultSettingsWorkspace({ name }: { name: string | undefined }) {
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
  const settingsMainRef = useRef<HTMLDivElement>(null);
  const generalErrorRef = useRef<HTMLDivElement>(null);
  const accessErrorRef = useRef<HTMLDivElement>(null);
  const requestedSection = location.hash.slice(1);
  const activeSection = SETTINGS_SECTIONS.some(
    ({ value }) => value === requestedSection,
  )
    ? (requestedSection as SettingsSection)
    : "general";
  const selectedSectionRef = useRef<string>(activeSection);
  const mountedRef = useRef(false);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  function selectSection(section: string) {
    // Pointer-down and focus can both activate a Radix tab before the URL
    // rerenders. Record that pending selection so one gesture adds one entry.
    if (selectedSectionRef.current === section) return;
    selectedSectionRef.current = section;
    navigate({
      pathname: location.pathname,
      search: location.search,
      hash: `#${section}`,
    });
  }
  const searchStatus = useOptionalSearchStatus();
  const vaultStatus = searchStatus?.observations.find(
    (row) => row.vaultName === name,
  );

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
        if (!mountedRef.current) return;
        setInfo(data);
        setDescription(data.description || "");
        setPublicAccess((data.public_access as PublicAccess) || "none");
      })
      .catch((error) => {
        if (mountedRef.current) setLoadError(error?.message || "Couldn't load this vault.");
      });
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
    selectedSectionRef.current = activeSection;
    if (settingsMainRef.current) settingsMainRef.current.scrollTop = 0;
  }, [activeSection]);

  useEffect(() => {
    if (saveError && saveErrorScope === activeSection) {
      (saveErrorScope === "general"
        ? generalErrorRef
        : accessErrorRef
      ).current?.focus();
    }
  }, [saveError, saveErrorScope, activeSection]);

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
      refetchVaults();
      if (!mountedRef.current) return;
      setInfo({ ...info, ...patch });
      setSavedScope(scope);
      requestAnimationFrame(() => {
        if (!mountedRef.current) return;
        const status = (
          scope === "general" ? generalStatusRef : accessStatusRef
        ).current;
        if (status && !status.closest("[hidden]")) status.focus();
      });
    } catch (error: unknown) {
      if (!mountedRef.current) return;
      setSaveError(error instanceof Error ? error.message : "Save failed");
      setSaveErrorScope(scope);
      selectSection(scope);
    } finally {
      if (mountedRef.current) setSavingScope(null);
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
      className="min-h-full bg-surface xl:h-full xl:min-h-0"
      contentClassName="min-h-full xl:flex xl:h-full xl:min-h-0 xl:flex-col"
    >
      <h1 className="sr-only">Settings</h1>
      <Tabs
        value={activeSection}
        onValueChange={selectSection}
        className="flex min-h-full min-w-0 flex-col xl:min-h-0 xl:flex-1"
        data-testid="settings-workspace-shell"
      >
        {info && !canEdit && (
          <Alert
            variant="info"
            className="shrink-0 rounded-none border-x-0 border-t-0 border-b px-3 py-2 sm:px-5 lg:px-6"
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
        <div className="shrink-0 border-b border-border sm:px-2 lg:px-3">
          <div className="overflow-x-auto">
            <TabsList
              aria-label="Vault settings sections"
              className="h-12 gap-0 rounded-none bg-transparent p-0"
            >
              {SETTINGS_SECTIONS.map(({ value, label }) => (
                <TabsTrigger
                  key={value}
                  value={value}
                  aria-description={
                    (value === "general" && descriptionDirty) ||
                    (value === "access" && accessDirty)
                      ? "Unsaved changes"
                      : undefined
                  }
                  className="h-11 rounded-none border-b-2 border-transparent! px-3 data-[state=active]:border-primary! data-[state=active]:bg-transparent data-[state=active]:text-link data-[state=active]:shadow-none"
                >
                  {label}
                  {((value === "general" && descriptionDirty) ||
                    (value === "access" && accessDirty)) && (
                    <span
                      className="h-1.5 w-1.5 rounded-full bg-current"
                      aria-hidden
                    />
                  )}
                </TabsTrigger>
              ))}
            </TabsList>
          </div>
        </div>
        <div
          ref={settingsMainRef}
          data-testid="settings-workspace-frame"
          className="@container/vault-settings rail-scroll min-w-0 flex-1 px-3 py-5 sm:px-5 lg:p-6 xl:min-h-0 xl:overflow-y-auto xl:overscroll-contain"
        >
          <div className="grid w-full max-w-[86rem] min-w-0 items-start gap-6 @[52rem]/vault-settings:grid-cols-[minmax(0,1fr)_18rem] @[72rem]/vault-settings:grid-cols-[minmax(0,1fr)_20rem]">
          <div className="min-w-0 w-full max-w-5xl">
            {loadError && (
              <Alert variant="destructive">
                {loadError}
                <div className="mt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => loadInfo(name)}
                  >
                    Try again
                  </Button>
                </div>
              </Alert>
            )}
            {loading && <SettingsLoadingState />}
            {info && (
              <>
                <TabsContent
                  value="general"
                  forceMount
                  hidden={activeSection !== "general"}
                  className="pt-0"
                >
                  <section
                    id="general"
                    role="region"
                    aria-labelledby="general-settings-heading"
                    className="scroll-mt-6"
                  >
                    <WorkspaceSectionHeader
                      id="general-settings-heading"
                      icon={Settings2}
                      title="General"
                      description="The identity people see across the workspace."
                      tone="knowledge"
                    />
                    <Panel variant="workspace" className="border-border-strong">
                      <div className="grid gap-4 p-4">
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
                          <div
                            ref={generalErrorRef}
                            tabIndex={-1}
                            className="outline-none"
                          >
                            <Alert variant="destructive">{saveError}</Alert>
                          </div>
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

                </TabsContent>
                <TabsContent
                  value="access"
                  forceMount
                  hidden={activeSection !== "access"}
                  className="pt-0"
                >
                  <section
                    id="access"
                    role="region"
                    aria-labelledby="access-settings-heading"
                    className="scroll-mt-6"
                  >
                    <WorkspaceSectionHeader
                      id="access-settings-heading"
                      icon={ShieldCheck}
                      title="Access"
                      description="Set the vault-wide default without changing individual member roles."
                      tone={publicAccess === "none" ? "info" : "success"}
                    />
                    <Panel variant="workspace" className="border-border-strong">
                      <div className="space-y-4 p-4">
                        <div>
                          <Label
                            id="public-access-label"
                            className="mb-2 block"
                          >
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
                          <div
                            ref={accessErrorRef}
                            tabIndex={-1}
                            className="outline-none"
                          >
                            <Alert variant="destructive">{saveError}</Alert>
                          </div>
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
                          <Link to={`/vault/${name}/members`}>
                            Open members
                          </Link>
                        </Button>
                      </div>
                    </Panel>
                  </section>
                </TabsContent>
                <TabsContent
                  value="skill"
                  forceMount
                  hidden={activeSection !== "skill"}
                  className="pt-0"
                >
                  <SkillSection
                    vault={name}
                    doc={skillDoc}
                    loading={skillQuery.isLoading || (!info && !loadError)}
                    isMirror={info.is_external_git}
                    canManage={canManageSkill}
                  />
                </TabsContent>
                <TabsContent
                  value="danger"
                  forceMount
                  hidden={activeSection !== "danger"}
                  className="space-y-6 pt-0"
                >
                  <section aria-labelledby="operations-heading">
                    <WorkspaceSectionHeader
                      id="operations-heading"
                      icon={Settings2}
                      title="Operations"
                      description="Background processing for this vault."
                      tone="neutral"
                    />
                    <div className="py-4">
                      <SearchStatusStages observation={vaultStatus} />
                      <p className="mt-4 text-xs leading-relaxed text-foreground-muted">
                        New content is processed asynchronously after each
                        write.
                      </p>
                    </div>
                  </section>
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
                              history, relations, embeddings, sessions, and
                              stored files. This cannot be undone.
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
                </TabsContent>
              </>
            )}
          </div>
          {info && <VaultDetailsPanel name={name} info={info} />}
          </div>
        </div>
      </Tabs>

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

function VaultDetailsPanel({ name, info }: { name: string; info: VaultInfo }) {
  const counts = [
    ["Members", info.member_count],
    ["Collections", info.collection_count],
    ["Documents", info.document_count],
    ["Tables", info.table_count],
    ["Files", info.file_count],
    ["Graph links", info.edge_count],
  ] as const;

  return (
    <aside className="min-w-0" aria-labelledby="vault-details-heading">
      <Panel variant="workspace" flush className="rounded-[var(--radius-sm)]">
        <div className="flex min-h-12 items-center gap-2.5 border-b border-border bg-background px-4 py-2.5">
          <TonalIcon tone="knowledge" size="sm"><Box aria-hidden /></TonalIcon>
          <h2 id="vault-details-heading" className="text-sm font-semibold text-foreground">Vault details</h2>
        </div>
        <div className="px-4 pb-2 pt-4">
          {(info.role || info.is_archived || info.is_external_git || (info.public_access && info.public_access !== "none")) && (
            <div className="mb-2 flex flex-wrap items-center gap-2">
              {info.role && <RoleBadge role={info.role} />}
              <VaultStateBadge archived={info.is_archived} externalGit={info.is_external_git} publicAccess={info.public_access} />
            </div>
          )}
          <dl className="divide-y divide-border">
            <ContextRow label="Owner" value={info.owner_display_name || info.owner || "Not available"} />
            <ContextRow label="Created" value={info.created_at ? timeAgo(info.created_at) : "Not available"} />
            <ContextRow label="Last active" value={info.last_activity ? timeAgo(info.last_activity) : "Not available"} />
          </dl>
        </div>
        <dl className="grid grid-cols-2 gap-x-5 gap-y-4 border-t border-border p-4">
          {counts.map(([label, count]) => (
            <div key={label} className="min-w-0">
              <dt className="text-xs text-foreground-muted">{label}</dt>
              <dd className="mt-1 break-words text-sm font-medium tabular-nums text-foreground">
                {typeof count === "number" ? count.toLocaleString() : "Not available"}
              </dd>
            </div>
          ))}
        </dl>
        <div className="border-t border-border p-3">
          <Link to={`/vault/${name}`} className="flex min-h-10 items-center justify-between gap-2 rounded-[var(--radius-sm)] px-2 text-sm font-medium text-link transition-token hover:bg-surface-hover hover:text-link-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
            Open Vault Overview
            <ChevronRight className="h-4 w-4 shrink-0" aria-hidden />
          </Link>
        </div>
      </Panel>
    </aside>
  );
}

function ContextRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-3 py-2.5 text-sm">
      <dt className="text-foreground-muted">{label}</dt>
      <dd className="min-w-0 text-right font-medium text-foreground [overflow-wrap:anywhere]">
        {value}
      </dd>
    </div>
  );
}

function SettingsLoadingState() {
  return (
    <div
      className="space-y-5"
      role="status"
      aria-label="Loading vault settings"
    >
      <div
        className="h-8 w-36 animate-pulse rounded-[var(--radius-sm)] bg-surface-2"
        aria-hidden
      />
      <div className="space-y-4">
        {[0, 1].map((index) => (
          <div
            key={index}
            className="h-32 animate-pulse rounded-[var(--radius-md)] bg-surface-2"
            aria-hidden
          />
        ))}
      </div>
    </div>
  );
}
