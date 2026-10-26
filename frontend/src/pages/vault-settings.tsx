import { useEffect, useRef, useState, type RefObject } from "react";
import { useLocation, useNavigate, useParams, type NavigateOptions } from "react-router-dom";
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  FileText,
  Lock,
  RotateCcw,
  Save,
  Settings,
  Trash2,
  ShieldCheck,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import {
  archiveVault,
  getDocument,
  getVaultInfo,
  unarchiveVault,
  updateVault,
} from "@/lib/api";
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
import { SettingRow, SettingsSection } from "@/components/ui/settings-section";
import { Textarea } from "@/components/ui/textarea";
import { WorkspaceSectionHeader } from "@/components/ui/workspace-section-header";
import { DeleteVaultDialog } from "@/components/delete-vault-dialog";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { useResourceNavigationGuard } from "@/contexts/resource-navigation-context";
import { useDocumentHistoryGuard } from "@/hooks/use-document-history-guard";
import { retainsGuideSettings } from "@/lib/vault-guide-navigation";

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
type PendingExit = { kind: "navigation"; href: string; options?: NavigateOptions } | { kind: "history"; proceed: () => void };
const SETTINGS_SECTIONS: Array<{ value: SettingsSection; label: string }> = [
  { value: "general", label: "Vault details" },
  { value: "access", label: "Access permissions" },
  { value: "skill", label: "Vault guide" },
  { value: "danger", label: "Advanced" },
];

const PUBLIC_LABELS: Record<PublicAccess, string> = {
  none: "Invited members only",
  reader: "Everyone can view",
  writer: "Everyone can edit",
};
const PUBLIC_DESCRIPTIONS: Record<PublicAccess, string> = {
  none: "Only people invited to this vault.",
  reader:
    "Signed-in AKB users can read without an invitation.",
  writer:
    "Signed-in AKB users can add, edit and delete content.",
};
const PUBLIC_ORDER: PublicAccess[] = ["none", "reader", "writer"];

export default function VaultSettingsPage() {
  const { name } = useParams<{ name: string }>();
  const user = useCurrentUser();
  // Proof revisions refresh reads, not the same account's active Guide draft.
  return <VaultSettingsWorkspace key={JSON.stringify([user?.user_id, name])} name={name} />;
}

function VaultSettingsWorkspace({ name }: { name: string | undefined }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { refetchVaults } = useVaultRefresh();
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  const [loadedRevision, setLoadedRevision] = useState<number | null>(null);
  const requestGeneration = useRef(0);
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const hasInfo = info !== null;
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
  const [guideDraft, setGuideDraft] = useState({ dirty: false, saving: false });
  const [pendingExit, setPendingExit] = useState<PendingExit | null>(null);
  const confirmExitFocus = useRef<HTMLElement | null>(null);
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
  const mountedRef = useRef(false);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  function selectSection(section: string) {
    if (location.hash === `#${section}`) return;
    navigate({
      pathname: location.pathname,
      search: location.search,
      hash: `#${section}`,
    });
  }

  const skillQuery = useQuery({
    queryKey: ["document", name, VAULT_SKILL_PATH, user?.user_id, revision],
    queryFn: () => getDocument(name!, VAULT_SKILL_PATH),
    retry: false,
    enabled: !checking && loadedRevision === revision && !!name && !!info && !info.is_external_git,
  });
  const skillMissing = skillQuery.isError &&
    typeof skillQuery.error === "object" && skillQuery.error !== null &&
    "status" in skillQuery.error && skillQuery.error.status === 404;
  const skillDoc = skillMissing ? null : skillQuery.data;
  const skillError = skillQuery.isError && !skillMissing
    ? skillQuery.error.message || "Couldn't load the vault guide."
    : undefined;

  function loadInfo(vault: string) {
    const generation = ++requestGeneration.current;
    setLoadError("");
    getVaultInfo(vault)
      .then((data) => {
        if (!mountedRef.current || generation !== requestGeneration.current) return;
        setLoadedRevision(revision);
        setInfo(data);
        setDescription(value => !info || value === (info.description || "") ? data.description || "" : value);
        setPublicAccess(value => !info || value === (info.public_access || "none") ? (data.public_access as PublicAccess) || "none" : value);
      })
      .catch((error) => {
        if (mountedRef.current && generation === requestGeneration.current) setLoadError(error?.message || "Couldn't load this vault.");
      });
  }

  useEffect(() => {
    if (!name || checking) return;
    setLoadError("");
    loadInfo(name);
    return () => { requestGeneration.current += 1; };
    // The workspace is account/vault keyed. Retain same-account drafts while
    // proof revision invalidates the private read and its late completions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, checking, revision]);

  useEffect(() => {
    if (!hasInfo || checking || loadedRevision !== revision || !requestedSection) return;
    const destination = document.getElementById(activeSection);
    if (!destination || !settingsMainRef.current) return;
    // Scroll the sheet, not its ancestor rails. Keep old deep links usable.
    const frame = settingsMainRef.current;
    const scroller = frame.scrollHeight > frame.clientHeight
      ? frame
      : frame.closest<HTMLElement>('[data-slot="vault-route-viewport"]');
    scroller?.scrollTo?.({ top: scroller.scrollTop + destination.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 16 });
  }, [activeSection, requestedSection, hasInfo, checking, loadedRevision, revision]);

  useEffect(() => {
    if (saveError && saveErrorScope) {
      (saveErrorScope === "general"
        ? generalErrorRef
        : accessErrorRef
      ).current?.focus();
    }
  }, [saveError, saveErrorScope]);

  useEffect(() => {
    if (!name) return;
    const previous = document.title;
    document.title = `${name} · Settings · AKB`;
    return () => {
      document.title = previous;
    };
  }, [name]);

  const verified = !checking && loadedRevision === revision && !loadError;
  const canEdit = verified && info?.role === "owner";
  const canManageSkill = verified && info?.role === "owner" && !info?.is_archived;
  const savedPublic = (info?.public_access as PublicAccess) || "none";
  const descriptionDirty = Boolean(
    info && description !== (info.description || ""),
  );
  const accessDirty = Boolean(info && publicAccess !== savedPublic);
  const dirty = descriptionDirty || accessDirty;
  const saving = savingScope !== null;
  const hasDraft = dirty || guideDraft.dirty;
  const busy = saving || guideDraft.saving;

  function requestExit(exit: PendingExit) {
    if (busy || (pendingExit && exit.kind !== "history")) return;
    if (!pendingExit) {
      const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      const triggerId = active?.closest('[role="menu"]')?.getAttribute("aria-labelledby");
      confirmExitFocus.current = (triggerId ? document.getElementById(triggerId) : null) ?? active;
    }
    setPendingExit(exit);
  }

  const continueNavigation = useResourceNavigationGuard(hasDraft || busy ? (href, options) => {
    // Anchors within this sheet never discard any of its drafts.
    if (new URL(href, window.location.origin).pathname === location.pathname) return true;
    if (name && retainsGuideSettings(name, location, href, options?.state)) return true;
    requestExit({ kind: "navigation", href, options });
    return false;
  } : null);
  useDocumentHistoryGuard(hasDraft || busy,
    proceed => requestExit({ kind: "history", proceed }),
    destination => destination.pathname !== location.pathname &&
      !(name && retainsGuideSettings(name, location, destination.href, window.history.state?.usr)));

  function confirmExit() {
    if (!pendingExit || busy) return;
    // Leave drafts intact until navigation actually unmounts this workspace.
    // A different active workspace guard may still cancel the departure.
    if (pendingExit.kind === "history") pendingExit.proceed();
    else continueNavigation(() => navigate(pendingExit.href, pendingExit.options));
  }

  useEffect(() => {
    if (!hasDraft && !busy) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [hasDraft, busy]);

  function requestSave(scope: SaveScope) {
    if (!info || !canEdit || busy) return;
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
    if (!name || !info || !canEdit || busy) return;
    const generation = requestGeneration.current;
    setSavingScope(scope);
    setSaveError("");
    setSaveErrorScope(null);
    try {
      const patch =
        scope === "general" ? { description } : { public_access: publicAccess };
      await updateVault(name, patch);
      refetchVaults();
      if (!mountedRef.current || generation !== requestGeneration.current) return;
      setInfo(current => current ? { ...current, ...patch } : current);
      setSavedScope(scope);
    } catch (error: unknown) {
      if (!mountedRef.current || generation !== requestGeneration.current) return;
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
    if (!name || !canEdit || busy) return;
    const generation = requestGeneration.current;
    await archiveVault(name);
    const data = await getVaultInfo(name);
    if (!mountedRef.current || generation !== requestGeneration.current) return;
    setInfo(data);
    refetchVaults();
  }

  async function confirmUnarchive() {
    if (!name || !canEdit || busy) return;
    const generation = requestGeneration.current;
    await unarchiveVault(name);
    const data = await getVaultInfo(name);
    if (!mountedRef.current || generation !== requestGeneration.current) return;
    setInfo(data);
    refetchVaults();
  }

  if (!name) return null;

  const loading = info === null && !loadError;

  return (
    <PageShell
      header={null}
      contentWidth="full"
      className="workspace-density-compact mx-auto w-full max-w-7xl"
    >
      <h1 className="sr-only">Settings</h1>
      <WorkspaceSectionHeader
        id="vault-settings-heading" icon={Settings} title="Settings"
        testId="vault-settings-header" compact
        right={verified && info?.is_archived ? <Badge variant="default">Archived</Badge> : undefined}
      />
      {info && !verified && (
        loadError ? (
          <Alert variant="destructive">
            {loadError}
            <Button variant="outline" size="sm" onClick={() => loadInfo(name)} disabled={checking}>Try again</Button>
          </Alert>
        ) : <SettingsLoadingState />
      )}
      <div
        hidden={!!info && !verified}
        inert={!!info && !verified}
        className="min-w-0"
        data-testid="settings-workspace-shell"
      >
        <Panel variant="workspace">
        {info && !canEdit && (
          <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3 text-ui text-foreground-muted sm:px-5">
            <Lock className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span>Read only — only the vault owner can change settings.</span>
            {info.role_source === "public" && <Badge variant="info-outline">Access via public policy</Badge>}
          </div>
        )}
        <div
          ref={settingsMainRef}
          data-testid="settings-workspace-frame"
          className="@container/vault-settings min-w-0"
        >
          {verified && busy && <p role="status" className="px-4 py-3 text-ui text-foreground-muted sm:px-5">Saving changes…</p>}
          {loadError && (
            <Alert variant="destructive" className="m-4 sm:m-5">
              {loadError}
              <div className="mt-3"><Button variant="outline" size="sm" onClick={() => loadInfo(name)}>Try again</Button></div>
            </Alert>
          )}
          {loading && <div className="p-4 sm:p-5"><SettingsLoadingState /></div>}
          {info && (
            <div className="divide-y divide-border">
              <SettingsSection id="general" title="Vault details" layout="form" className="gap-4"
                icon={<FileText aria-hidden />} description="Describe this vault and copy its address.">
                <div className="grid min-w-0 max-w-5xl gap-4 @[44rem]/settings-values:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
                  <div className="min-w-0" data-testid="settings-general-editor">
                    <Label htmlFor="vault-description" className="mb-2 block text-ui">Description</Label>
                    <Textarea
                      id="vault-description"
                      value={description}
                      onChange={event => { setDescription(event.target.value); setSavedScope(null); }}
                      readOnly={!canEdit}
                      disabled={busy}
                      placeholder="What belongs in this vault?"
                      rows={3}
                      className="min-h-20 resize-y text-base leading-relaxed sm:text-ui"
                    />
                    {saveErrorScope === "general" && (
                      <div ref={generalErrorRef} tabIndex={-1} className="mt-3 outline-none">
                        <Alert variant="destructive">{saveError}</Alert>
                      </div>
                    )}
                    {canEdit && <SettingsActionBar
                      dirty={descriptionDirty} saving={savingScope === "general"} disabled={busy}
                      saved={savedScope === "general" && !descriptionDirty} statusRef={generalStatusRef}
                      onSave={() => requestSave("general")} onDiscard={() => handleDiscard("general")}
                    />}
                  </div>
                  <div className="min-w-0">
                    <p id="vault-address-label" className="mb-2 text-ui font-medium">Vault address</p>
                    <div role="group" aria-labelledby="vault-address-label" className="flex min-h-11 min-w-0 items-center gap-2 rounded-[var(--radius-md)] border border-border bg-background pl-3 pr-1 sm:min-h-9">
                      <code className="min-w-0 flex-1 text-ui text-foreground [overflow-wrap:anywhere]">akb://{name}</code>
                      <CopyButton className="workspace-icon-control h-11 w-11 hover:bg-surface-hover" value={`akb://${name}`} label="Copy vault address" />
                    </div>
                    <p className="mt-2 text-xs text-foreground-muted">Permanent</p>
                  </div>
                </div>
              </SettingsSection>

              <SettingsSection id="access" title="Access permissions" layout="form" tone="people" className="gap-4"
                icon={<ShieldCheck aria-hidden />} description="Choose who can open this vault and what they can do.">
                <div data-testid="settings-access-editor" className="max-w-5xl">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <p id="vault-access-label" className="text-ui font-medium">Who can access</p>
                    <span className="inline-flex items-center gap-1.5 text-xs text-foreground-muted">
                      <Lock className="h-3 w-3" aria-hidden />Sign-in required
                    </span>
                  </div>
                  <fieldset role="radiogroup" aria-labelledby="vault-access-label" aria-describedby="vault-access-help"
                    disabled={!canEdit || busy} className="grid divide-y divide-border overflow-hidden rounded-[var(--radius-md)] border border-border @[44rem]/settings-values:grid-cols-3 @[44rem]/settings-values:divide-x @[44rem]/settings-values:divide-y-0">
                    {PUBLIC_ORDER.map(value => {
                      const selected = publicAccess === value;
                      const helpId = selected ? "vault-access-help" : `vault-access-${value}-help`;
                      return (
                        <label key={value} className={`flex min-h-14 items-start gap-2.5 px-3 py-3 text-ui ${selected ? "bg-surface-selected text-surface-selected-foreground" : "bg-surface text-foreground hover:bg-surface-hover"} ${canEdit && !busy ? "cursor-pointer" : ""}`}>
                          <input type="radio" name="vault-default-access" value={value} checked={selected}
                            aria-labelledby={`vault-access-${value}-label`} aria-describedby={helpId}
                            onChange={() => { setPublicAccess(value); setSavedScope(null); }}
                            className="mt-0.5 h-4 w-4 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ring" />
                          <span className="min-w-0 flex-1">
                            <span className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                              <span id={`vault-access-${value}-label`} className="font-medium">{PUBLIC_LABELS[value]}</span>
                              {value === savedPublic ? <span className="text-xs font-normal text-foreground-muted">Current</span>
                                : selected && <span className="text-xs font-normal text-link">Not saved</span>}
                            </span>
                            <span id={helpId} className={`mt-1 flex items-start gap-1.5 text-xs leading-relaxed ${selected && value === "writer" ? "text-warning-soft-foreground" : "text-foreground-muted"}`}>
                              {selected && value === "writer" && <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />}
                              {PUBLIC_DESCRIPTIONS[value]}
                            </span>
                          </span>
                        </label>
                      );
                    })}
                  </fieldset>
                  {saveErrorScope === "access" && (
                    <div ref={accessErrorRef} tabIndex={-1} className="mt-3 outline-none">
                      <Alert variant="destructive">{saveError}</Alert>
                    </div>
                  )}
                  {canEdit && <SettingsActionBar
                    dirty={accessDirty} saving={savingScope === "access"} disabled={busy}
                    saved={savedScope === "access" && !accessDirty} statusRef={accessStatusRef}
                    onSave={() => requestSave("access")} onDiscard={() => handleDiscard("access")}
                  />}
                </div>
              </SettingsSection>

              <SkillSection
                vault={name} doc={skillDoc} loading={skillQuery.isLoading || (!info && !loadError)}
                error={skillError} onRetry={() => { void skillQuery.refetch(); }}
                isMirror={info.is_external_git} canManage={canManageSkill} busy={saving}
                queryKey={["document", name, VAULT_SKILL_PATH, user?.user_id, revision]}
                navigationManaged onDraftStateChange={setGuideDraft}
              />

              {canEdit && (
                <SettingsSection id="danger" title="Danger zone">
                  <SettingRow label={info.is_archived ? "Vault archived" : "Archive vault"}>
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <p className="text-ui text-foreground-muted">{info.is_archived ? "Read-only. Restore writing at any time." : "Make this vault read-only. You can restore it later."}</p>
                      <Button variant="outline" size="sm" disabled={busy} onClick={() => info.is_archived ? setPendingUnarchive(true) : setPendingArchive(true)} className="min-h-10 shrink-0 sm:min-h-8">
                        {info.is_archived ? <RotateCcw className="h-3.5 w-3.5" aria-hidden /> : <Archive className="h-3.5 w-3.5" aria-hidden />}
                        {info.is_archived ? "Unarchive" : "Archive"}
                      </Button>
                    </div>
                  </SettingRow>
                  <SettingRow label="Delete vault permanently">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <p className="text-ui text-foreground-muted">All vault data will be removed. This cannot be undone.</p>
                      <Button variant="outline" size="sm" className="min-h-10 shrink-0 border-destructive text-destructive hover:bg-destructive-soft sm:min-h-8" disabled={busy} onClick={() => setDeleteOpen(true)}>
                        <Trash2 className="h-3.5 w-3.5" aria-hidden />Delete vault
                      </Button>
                    </div>
                  </SettingRow>
                </SettingsSection>
              )}
            </div>
          )}
        </div>
        </Panel>
      </div>

      <span className="sr-only" role="status" aria-live="polite">
        {loading
          ? "Loading vault settings"
          : loadError
            ? "Could not load settings"
            : ""}
      </span>

      <ConfirmDialog
        open={pendingExit !== null}
        onOpenChange={open => {
          if (open || !mountedRef.current) return;
          if (!confirmExitFocus.current?.isConnected) {
            confirmExitFocus.current = settingsMainRef.current?.querySelector<HTMLElement>("#vault-description") ?? null;
          }
          setPendingExit(null);
        }}
        returnFocusRef={confirmExitFocus}
        title="Discard unsaved settings changes?"
        description="Your unsaved description, public-access, and vault guide changes will be discarded when you leave."
        confirmLabel="Discard changes and leave"
        cancelLabel="Keep editing"
        variant="destructive"
        busy={busy}
        onConfirm={confirmExit}
      />
      <ConfirmDialog
        open={verified && pendingArchive}
        onOpenChange={setPendingArchive}
        title={`Archive "${name}"?`}
        description={
          "Documents and tables become read-only. Agents can recall but cannot write.\nYou can unarchive any time."
        }
        confirmLabel="Archive vault"
        onConfirm={confirmArchive}
      />
      <ConfirmDialog
        open={verified && pendingUnarchive}
        onOpenChange={setPendingUnarchive}
        title={`Unarchive "${name}"?`}
        description="The vault returns to active. Agents can write again."
        confirmLabel="Unarchive"
        onConfirm={confirmUnarchive}
      />
      <ConfirmDialog
        open={verified && pendingPublicWrite}
        onOpenChange={setPendingPublicWrite}
        title={`Let everyone edit "${name}"?`}
        variant="destructive"
        description={
          <span className="flex items-start gap-2">
            <AlertTriangle
              className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
              aria-hidden
            />
            <span>
              Anyone signed in to this AKB will be able to add, edit, and delete
              content, even if they aren’t a member. You can change this setting
              later.
            </span>
          </span>
        }
        confirmLabel="Allow editing for everyone"
        onConfirm={() => doSave("access")}
      />

      <DeleteVaultDialog
        open={verified && deleteOpen}
        onOpenChange={setDeleteOpen}
        vault={name}
        inventory={{ documents: info?.document_count, tables: info?.table_count, files: info?.file_count }}
        onDeleted={() => {
          if (!mountedRef.current || !verified) return;
          refetchVaults();
          continueNavigation(() => navigate("/vault"));
        }}
      />
    </PageShell>
  );
}

function SettingsActionBar({
  dirty,
  saving,
  saved,
  disabled,
  statusRef,
  onSave,
  onDiscard,
}: {
  dirty: boolean;
  saving: boolean;
  saved: boolean;
  disabled: boolean;
  statusRef: RefObject<HTMLSpanElement | null>;
  onSave: () => void;
  onDiscard: () => void;
}) {
  if (!dirty && !saving && !saved) return null;
  return (
    <div className="mt-3 flex min-h-8 flex-wrap items-center gap-2">
      {(dirty || saving) && <Button
        type="button"
        size="sm"
        onClick={onSave}
        loading={saving}
        disabled={!dirty || disabled}
        className="min-h-10 sm:min-h-8"
      >
        {!saving && <Save className="h-3.5 w-3.5" aria-hidden />}
        {saving ? "Saving…" : "Save changes"}
      </Button>}
      {dirty && !saving && (
        <Button type="button" variant="outline" size="sm" onClick={onDiscard} disabled={disabled} className="min-h-10 sm:min-h-8">
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

function SettingsLoadingState() {
  return (
    <div role="status" aria-label="Loading vault settings" className="space-y-6">
      {[0, 1, 2].map(index => (
        <div key={index} aria-hidden className="space-y-3">
          <div className="h-5 w-28 animate-pulse rounded-[var(--radius-sm)] bg-surface-2" />
          <div className="h-px bg-border" />
          <div className="h-20 max-w-5xl animate-pulse rounded-[var(--radius-sm)] bg-surface-2" />
        </div>
      ))}
    </div>
  );
}
