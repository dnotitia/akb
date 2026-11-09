import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { Link, useLocation, useNavigate, useParams, type NavigateOptions } from "react-router-dom";
import {
  AlertTriangle,
  Archive,
  ArrowUpRight,
  BookOpen,
  Box,
  CheckCircle2,
  FileText,
  Lock,
  RotateCcw,
  Save,
  CircleDot,
  Trash2,
  ShieldCheck,
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
import { SkillSection } from "@/components/skill/skill-section";
import { VAULT_SKILL_PATH } from "@/lib/skill";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { CopyButton } from "@/components/ui/copy-button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
const SETTINGS_SECTIONS: Array<{ value: SettingsSection; label: string; shortLabel: string; icon: LucideIcon }> = [
  { value: "general", label: "Vault details", shortLabel: "Details", icon: FileText },
  { value: "access", label: "Access permissions", shortLabel: "Access", icon: ShieldCheck },
  { value: "skill", label: "Vault guide", shortLabel: "Guide", icon: BookOpen },
  { value: "danger", label: "Lifecycle actions", shortLabel: "Lifecycle", icon: Archive },
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
  const locationRef = useRef(location);
  const navigationHashRef = useRef(location.hash);
  if (locationRef.current.key !== location.key) navigationHashRef.current = location.hash;
  locationRef.current = location;
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
  const sectionNavigationRef = useRef<HTMLDivElement>(null);
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
    return () => {
      mountedRef.current = false;
    };
  }, []);

  function selectSection(section: string) {
    const current = locationRef.current;
    if (navigationHashRef.current === `#${section}`) return;
    // Pointer-down, keyboard activation and an assistive click can describe
    // the same activation before the route transition has rendered.
    navigationHashRef.current = `#${section}`;
    navigate({
      pathname: current.pathname,
      search: current.search,
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

  useLayoutEffect(() => {
    if (!hasInfo || checking || loadedRevision !== revision || !settingsMainRef.current) return;
    revealSettingsPanel(settingsMainRef.current, sectionNavigationRef.current);
  }, [activeSection, requestedSection, hasInfo, checking, loadedRevision, revision]);

  useEffect(() => {
    if (saveError && saveErrorScope && activeSection === saveErrorScope) {
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
  const selectedSection = activeSection === "danger" && !canEdit ? "general" : activeSection;
  const sectionDrafts: Partial<Record<SettingsSection, boolean>> = {
    general: descriptionDirty,
    access: accessDirty,
    skill: guideDraft.dirty,
  };

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
    // Tab changes within this workspace never discard any of its drafts.
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
    <div className="vault-control workspace-density-compact @container/vault-control">
      <header className="vault-control-header" data-testid="vault-settings-header">
        <div className="vault-control-identity">
          <span className="vault-control-mark"><Box aria-hidden /></span>
          <div className="min-w-0"><p className="vault-control-name">{name}</p><h1 id="vault-settings-heading">Vault settings</h1></div>
        </div>
        {verified && info && <div className="vault-control-status">
          {info.is_archived ? <Badge variant="outline"><Archive className="h-3 w-3" aria-hidden />Archived</Badge>
            : info.is_archived === false && <Badge variant="outline"><CheckCircle2 className="h-3 w-3" aria-hidden />Active</Badge>}
          <Badge variant="outline">{canEdit ? <ShieldCheck className="h-3 w-3" aria-hidden /> : <Lock className="h-3 w-3" aria-hidden />}{canEdit ? "Owner" : "Read-only settings"}</Badge>
          {hasDraft && <span className="vault-control-draft"><CircleDot aria-hidden />Unsaved changes</span>}
        </div>}
      </header>
      {info && !verified && (
        loadError ? <Alert variant="destructive">{loadError}<Button variant="outline" size="sm" onClick={() => loadInfo(name)} disabled={checking}>Try again</Button></Alert> : <SettingsLoadingState />
      )}
      <div hidden={!!info && !verified} inert={!!info && !verified} className="min-w-0" data-testid="settings-workspace-shell">
        {info && !canEdit && <Alert variant="info" className="mb-4"><Lock className="h-4 w-4" aria-hidden /><span>Read only — only the vault owner can change settings.</span>{info.role_source === "public" && <Badge variant="info-outline">Access via public policy</Badge>}</Alert>}
        <Tabs value={selectedSection} onValueChange={selectSection} activationMode="manual">
          <TabsList ref={sectionNavigationRef} className="vault-control-navigation" aria-label="Vault settings sections">
            {SETTINGS_SECTIONS.filter(section => section.value !== "danger" || canEdit).map(({ value, label, shortLabel, icon: Icon }) => <TabsTrigger
              key={value} value={value} disabled={!verified} aria-label={label}
              aria-describedby={sectionDrafts[value] ? `vault-settings-${value}-draft` : undefined}
              onClick={() => selectSection(value)}
            ><Icon aria-hidden /><span>{shortLabel}</span>{sectionDrafts[value] && <>
              <span className="vault-control-tab-draft" aria-hidden />
              <span id={`vault-settings-${value}-draft`} className="sr-only">Unsaved changes</span>
            </>}</TabsTrigger>)}
          </TabsList>
          {verified && busy && <p role="status" className="vault-control-progress">Saving changes…</p>}
          {loadError && <Alert variant="destructive" className="mb-4">{loadError}<div className="mt-3"><Button variant="outline" size="sm" onClick={() => loadInfo(name)}>Try again</Button></div></Alert>}
          <div ref={settingsMainRef} data-testid="settings-workspace-frame" className="vault-control-grid @container/vault-settings">
            {loading && <SettingsLoadingState />}
            {info && <>
              <TabsContent value="general" forceMount hidden={selectedSection !== "general"} inert={selectedSection !== "general"} className="vault-control-panel">
                <VaultControlCard id="general" title="Vault details" description="Give this space a clear purpose." icon={FileText}>
                  <div className="vault-control-details-fields">
                    <div data-testid="settings-general-editor">
                      <Label htmlFor="vault-description" className="vault-control-label">Description</Label>
                      <Textarea id="vault-description" value={description} onChange={event => { setDescription(event.target.value); setSavedScope(null); }}
                        readOnly={!canEdit} disabled={busy} placeholder="What belongs in this vault?" rows={4} className="vault-control-description" />
                      <p className="vault-control-help">Help people understand what to keep here.</p>
                      {saveErrorScope === "general" && <div ref={generalErrorRef} tabIndex={-1} className="mt-3 outline-none"><Alert variant="destructive">{saveError}</Alert></div>}
                      {canEdit && <SettingsActionBar dirty={descriptionDirty} saving={savingScope === "general"} disabled={busy}
                        saved={savedScope === "general" && !descriptionDirty} statusRef={generalStatusRef} onSave={() => requestSave("general")} onDiscard={() => handleDiscard("general")} />}
                    </div>
                    <div className="vault-control-address">
                      <div className="flex items-center justify-between gap-3"><p id="vault-address-label" className="vault-control-label">Vault address</p><span className="vault-control-help">Permanent</span></div>
                      <div role="group" aria-labelledby="vault-address-label" className="vault-control-address-value"><code>akb://{name}</code><CopyButton className="workspace-icon-control" value={`akb://${name}`} label="Copy vault address" /></div>
                    </div>
                  </div>
                </VaultControlCard>
              </TabsContent>

              <TabsContent value="access" forceMount hidden={selectedSection !== "access"} inert={selectedSection !== "access"} className="vault-control-panel">
                <VaultControlCard id="access" title="Access permissions" description="Choose the default access for signed-in users." icon={ShieldCheck}>
                  <div data-testid="settings-access-editor">
                    <div className="vault-control-field-title"><p id="vault-access-label" className="vault-control-label">Who can access</p><span><Lock aria-hidden />Sign-in required</span></div>
                    <fieldset role="radiogroup" aria-labelledby="vault-access-label" aria-describedby="vault-access-help" disabled={!canEdit || busy} className="vault-control-policies">
                      {PUBLIC_ORDER.map(value => {
                        const selected = publicAccess === value;
                        const helpId = selected ? "vault-access-help" : `vault-access-${value}-help`;
                        const Icon = value === "none" ? Lock : value === "writer" ? FileText : BookOpen;
                        return <label key={value} className="vault-control-policy" data-selected={selected} data-editable={canEdit && !busy}>
                          <input type="radio" name="vault-default-access" value={value} checked={selected} aria-labelledby={`vault-access-${value}-label`} aria-describedby={helpId}
                            onChange={() => { setPublicAccess(value); setSavedScope(null); }} />
                          <span className="vault-control-policy-icon"><Icon aria-hidden /></span>
                          <span className="min-w-0 flex-1"><span className="vault-control-policy-title"><span id={`vault-access-${value}-label`}>{PUBLIC_LABELS[value]}</span>
                            {value === savedPublic ? <span className="vault-control-policy-current">Current</span> : selected && <span className="vault-control-policy-current">Not saved</span>}</span>
                            <span id={helpId} className="vault-control-policy-description">{selected && value === "writer" && <AlertTriangle aria-hidden />}{PUBLIC_DESCRIPTIONS[value]}</span>
                          </span>
                        </label>;
                      })}
                    </fieldset>
                    <p className="vault-control-help mt-3">These defaults apply inside AKB. Publications have their own access controls.</p>
                    {saveErrorScope === "access" && <div ref={accessErrorRef} tabIndex={-1} className="mt-3 outline-none"><Alert variant="destructive">{saveError}</Alert></div>}
                    <div className="vault-control-access-footer">
                      {canEdit && <SettingsActionBar dirty={accessDirty} saving={savingScope === "access"} disabled={busy} saved={savedScope === "access" && !accessDirty}
                        statusRef={accessStatusRef} onSave={() => requestSave("access")} onDiscard={() => handleDiscard("access")} />}
                      <Link to={`/vault/${encodeURIComponent(name)}/members`} className="vault-control-members">Manage members<ArrowUpRight aria-hidden /></Link>
                    </div>
                  </div>
                </VaultControlCard>
              </TabsContent>

              <TabsContent value="skill" forceMount hidden={selectedSection !== "skill"} inert={selectedSection !== "skill"} className="vault-control-panel">
                <div className="vault-control-guide">
                  <SkillSection vault={name} doc={skillDoc} loading={skillQuery.isLoading || (!info && !loadError)} error={skillError} onRetry={() => { void skillQuery.refetch(); }}
                    isMirror={info.is_external_git} canManage={canManageSkill} busy={saving} headingLevel={2} editInReader
                    queryKey={["document", name, VAULT_SKILL_PATH, user?.user_id, revision]} navigationManaged onDraftStateChange={setGuideDraft} />
                </div>
              </TabsContent>

              {canEdit && <TabsContent value="danger" forceMount hidden={selectedSection !== "danger"} inert={selectedSection !== "danger"} className="vault-control-panel">
                <VaultControlCard id="danger" title="Danger zone" description="Changes to the availability and lifetime of this vault." icon={Archive} className="vault-control-lifecycle">
                  <div className="vault-control-lifecycle-row"><div><h3>{info.is_archived ? "Vault archived" : "Archive vault"}</h3><p>{info.is_archived ? "Read-only. Restore writing at any time." : "Make this vault read-only. You can restore it later."}</p></div>
                    <Button variant="outline" size="sm" disabled={busy} onClick={() => info.is_archived ? setPendingUnarchive(true) : setPendingArchive(true)}>
                      {info.is_archived ? <RotateCcw className="h-3.5 w-3.5" aria-hidden /> : <Archive className="h-3.5 w-3.5" aria-hidden />}{info.is_archived ? "Unarchive" : "Archive"}
                    </Button>
                  </div>
                  <div className="vault-control-lifecycle-row vault-control-delete"><div><h3>Delete vault permanently</h3><p>All vault data will be removed. This cannot be undone.</p></div>
                    <Button variant="outline" size="sm" className="border-destructive text-destructive hover:bg-destructive-soft" disabled={busy} onClick={() => setDeleteOpen(true)}><Trash2 className="h-3.5 w-3.5" aria-hidden />Delete vault</Button>
                  </div>
                </VaultControlCard>
              </TabsContent>}
            </>}
          </div>
        </Tabs>
        {savedScope && selectedSection !== savedScope && <span className="sr-only" role="status">
          {savedScope === "general" ? "Vault details saved." : "Access permissions saved."}
        </span>}
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
    </div>
  );
}

/** Switching from a long panel keeps its replacement below the sticky tabs. */
function revealSettingsPanel(frame: HTMLElement, navigation: HTMLElement | null) {
  const scroller = frame.closest<HTMLElement>('[data-slot="vault-route-viewport"]');
  if (!scroller) return;
  const offset = (navigation?.offsetHeight ?? 0) + 16;
  const delta = frame.getBoundingClientRect().top - scroller.getBoundingClientRect().top - offset;
  if (delta < 0) scroller.scrollTo?.({ top: Math.max(0, scroller.scrollTop + delta), behavior: "instant" });
}

function VaultControlCard({ id, title, description, icon: Icon, children, className = "" }: {
  id: string; title: string; description: string; icon: LucideIcon; children: ReactNode; className?: string;
}) {
  return <section id={id} role="region" aria-labelledby={`settings-${id}-heading`} className={`vault-control-card ${className}`}>
    <header className="vault-control-card-heading"><span><Icon aria-hidden /></span><div><h2 id={`settings-${id}-heading`} tabIndex={-1}>{title}</h2><p>{description}</p></div></header>
    <div data-slot="settings-group" className="vault-control-card-body">{children}</div>
  </section>;
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
    <div role="status" aria-label="Loading vault settings" className="vault-control-loading vault-control-grid">
      <div aria-hidden className="vault-control-card">
        <div className="vault-control-card-heading"><div className="h-5 w-28 animate-pulse rounded-[var(--radius-sm)] bg-surface-2" /></div>
        <div className="vault-control-card-body space-y-3">
          <div className="h-5 w-24 animate-pulse rounded-[var(--radius-sm)] bg-surface-2" />
          <div className="h-28 animate-pulse rounded-[var(--radius-sm)] bg-surface-2" />
        </div>
      </div>
    </div>
  );
}
