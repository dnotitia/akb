import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, type NavigateOptions } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { History, MoreHorizontal, RotateCcw, Save } from "lucide-react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { AgentPreview } from "./agent-preview";
import { DocumentConflictNotice } from "@/components/document-conflict-notice";
import { MarkdownRender } from "@/components/markdown-render";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadingState } from "@/components/ui/loading-state";
import { Textarea } from "@/components/ui/textarea";
import { SettingRow, SettingsSection } from "@/components/ui/settings-section";
import { ApiError, authSessionSnapshot, getDocument, getSkillTemplate, isCurrentAuthSession, updateDocument } from "@/lib/api";
import type { DocumentConflictResolution, DocumentConflictResult, DocumentConflictSnapshot } from "@/lib/document-conflict";
import { VAULT_SKILL_PATH } from "@/lib/skill";
import { timeAgo } from "@/lib/utils";
import { useResourceNavigation, useResourceNavigationGuard } from "@/contexts/resource-navigation-context";
import { useDocumentHistoryGuard } from "@/hooks/use-document-history-guard";
import { useDocumentHead } from "@/hooks/use-document-head";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";

interface SkillDoc {
  content?: string;
  updated_at?: string;
  current_commit?: string | null;
}

interface GuideComparison {
  scope: string;
  base: DocumentConflictSnapshot;
  local: DocumentConflictSnapshot;
  latest: DocumentConflictSnapshot | null;
  latestError?: string;
  observedCommit: string | null;
  resolution?: DocumentConflictResolution;
}

function validRevision(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function terminalFailure(error: unknown): error is ApiError {
  return error instanceof ApiError && [401, 403, 404].includes(error.status);
}

function guideSnapshot(label: string, commit: string | null, body: string): DocumentConflictSnapshot {
  return { label, commit, title: "Vault guide", body };
}

function guideExcerpt(content = "") {
  return content.slice(0, 2000)
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/[#>*_`]/g, "")
    .replace(/\s+/g, " ").trim().slice(0, 240);
}

interface Props {
  vault: string;
  /** The canonical doc from the settings page's skillQuery. */
  doc?: SkillDoc | null;
  loading?: boolean;
  error?: string;
  onRetry?: () => void;
  /** External-git mirror vaults never carry a vault guide (backend excludes
   *  them from the seed and the reservation backfill). */
  isMirror?: boolean;
  /** Owner-authorized management capability. Read surfaces remain visible. */
  canManage: boolean;
  /** Another setting scope is writing; serialize writes without hiding drafts. */
  busy?: boolean;
  queryKey?: readonly unknown[];
  /** A containing workspace can protect all its drafts with one confirmation. */
  navigationManaged?: boolean;
  onDraftStateChange?: (state: { dirty: boolean; saving: boolean }) => void;
}

/**
 * The one editing surface for the vault guide. The document viewer bounces the
 * canonical path here, so preview / agent view / edit / reset / history all
 * live in this section.
 *
 * The body editor is deliberately body-only rather than the document page's
 * FrontmatterEditDialog: that dialog PATCHes `type` along with the body, and
 * `skill` is no longer in DOC_TYPES, so it would send `type: "note"` and the
 * backend's pinned-type guard would reject the save. Title/type/tags on this
 * doc are system-managed anyway.
 */
export function SkillSection({
  vault,
  doc,
  loading,
  error,
  onRetry,
  isMirror,
  canManage,
  busy = false,
  queryKey = ["document", vault, VAULT_SKILL_PATH],
  navigationManaged = false,
  onDraftStateChange,
}: Props) {
  const { checking, revision: accessRevision } = useAccessVerification();
  const user = useCurrentUser();
  const ownerScope = JSON.stringify([vault, user?.user_id]);
  const observerScope = JSON.stringify([user?.user_id, accessRevision]);
  const comparisonScope = JSON.stringify([vault, observerScope]);
  const latestComparisonScope = useRef(comparisonScope);
  latestComparisonScope.current = comparisonScope;
  const latestScope = useRef(ownerScope);
  latestScope.current = ownerScope;
  const latestChecking = useRef(checking);
  latestChecking.current = checking;
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const { requestNavigation } = useResourceNavigation();
  const [tab, setTab] = useState("preview");
  const [expanded, setExpanded] = useState(false);
  const sectionRef = useRef<HTMLElement>(null);
  const editButtonRef = useRef<HTMLButtonElement>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saveConflict, setSaveConflict] = useState(false);
  const [saved, setSaved] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [baseCommit, setBaseCommit] = useState<string | null>(null);
  const [baseBody, setBaseBody] = useState<string | null>(null);
  const [comparison, setComparison] = useState<GuideComparison | null>(null);
  const [loadingComparison, setLoadingComparison] = useState(false);
  const comparisonGeneration = useRef(0);
  const comparisonController = useRef<AbortController | null>(null);
  const [guideUnavailable, setGuideUnavailable] = useState<{
    scope: string;
    verificationScope: string;
    operation: "read" | "write";
    error: ApiError;
  } | null>(null);
  // A static parent snapshot can lag behind the server snapshot the user just
  // reviewed. Ignore that exact old prop revision after applying the result.
  const [comparedBase, setComparedBase] = useState<{ priorCommit: string | null; commit: string } | null>(null);
  const [resetBaseCommit, setResetBaseCommit] = useState<string | null>(null);
  const [pendingExit, setPendingExit] = useState<
    { kind: "cancel" } | { kind: "navigation"; href: string; options?: NavigateOptions } | { kind: "history"; proceed: () => void } | null
  >(null);
  const dirty = baseBody !== null && draft !== baseBody;
  useEffect(() => { onDraftStateChange?.({ dirty, saving }); }, [dirty, saving, onDraftStateChange]);
  const observedHead = useDocumentHead({
    vault, document: VAULT_SKILL_PATH, scope: observerScope, baseCommit,
    enabled: expanded && tab === "edit" && canManage && !checking && !busy && !saving,
  });
  const unavailable = terminalFailure(observedHead.error) ? observedHead.error
    : guideUnavailable?.scope === ownerScope ? guideUnavailable.error : null;
  const propCommit = doc?.current_commit || null;
  const documentCommit = comparedBase?.priorCommit === propCommit ? comparedBase.commit : propCommit;
  const remoteCommit = observedHead.head?.current_commit && observedHead.head.current_commit !== baseCommit
    ? observedHead.head.current_commit : documentCommit;
  const changedElsewhere = saveConflict || (baseBody !== null && validRevision(baseCommit) && validRevision(remoteCommit) && baseCommit !== remoteCommit);
  const activeComparison = comparison?.scope === ownerScope ? comparison : null;

  useEffect(() => {
    comparisonGeneration.current += 1;
    comparisonController.current?.abort();
    comparisonController.current = null;
    setLoadingComparison(false);
    return () => { comparisonController.current?.abort(); };
  }, [comparisonScope, checking, canManage]);
  useEffect(() => { setComparison(null); }, [ownerScope]);
  useEffect(() => {
    if (observedHead.head && !observedHead.error && !observedHead.checking) {
      // A reader-authorized head proves availability, never owner capability.
      setGuideUnavailable(current => current?.operation === "write" ? current : null);
    }
  }, [observedHead.head, observedHead.error, observedHead.checking]);
  useEffect(() => {
    if (checking || !canManage) return;
    setGuideUnavailable(current => current?.scope === ownerScope && current.operation === "write" &&
      current.verificationScope !== comparisonScope ? null : current);
  }, [ownerScope, comparisonScope, checking, canManage]);
  const guardNavigation = useCallback((href: string, options?: NavigateOptions) => {
    // Settings anchors retain this mounted editor and its draft.
    if (new URL(href, window.location.origin).pathname === location.pathname) return true;
    setPendingExit({ kind: "navigation", href, options });
    return false;
  }, [location.pathname]);
  const continueNavigation = useResourceNavigationGuard(!navigationManaged && (dirty || saving) ? guardNavigation : null);
  useDocumentHistoryGuard(!navigationManaged && (dirty || saving), (proceed) => setPendingExit({ kind: "history", proceed }));
  useEffect(() => {
    if (!dirty && !saving) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty, saving]);

  function invalidate() {
    queryClient.invalidateQueries({
      queryKey: ["document", vault, VAULT_SKILL_PATH],
    });
    queryClient.invalidateQueries({ queryKey: ["vault-skill-preview", vault] });
  }

  // Read views do not own the draft. A dirty buffer keeps its original revision
  // pin even when a background read discovers another writer's changes.
  function selectTab(next: string) {
    if (next === "edit" && !dirty && !saving) {
      setDraft(doc?.content || "");
      setBaseBody(doc?.content || "");
      setBaseCommit(doc?.current_commit || null);
      setSaveError("");
      setSaveConflict(false);
      setComparedBase(null);
      setSaved(false);
    }
    setTab(next);
    setExpanded(true);
  }

  function discardDraft() {
    comparisonGeneration.current += 1;
    comparisonController.current?.abort();
    comparisonController.current = null;
    setComparison(null);
    setLoadingComparison(false);
    setDraft("");
    setBaseBody(null);
    setBaseCommit(null);
    setSaveError("");
    setSaveConflict(false);
    setComparedBase(null);
    setGuideUnavailable(null);
  }

  function confirmExit() {
    if (!pendingExit || saving) return;
    discardDraft();
    setTab("preview");
    setExpanded(false);
    if (pendingExit.kind === "navigation") continueNavigation(() => navigate(pendingExit.href, pendingExit.options));
    else if (pendingExit.kind === "history") pendingExit.proceed();
    else restoreEditFocus();
  }

  function restoreEditFocus() {
    // The edit trigger is disabled until the new collapsed view commits.
    requestAnimationFrame(() => { if (mounted.current) editButtonRef.current?.focus(); });
  }

  async function loadComparison() {
    const generation = ++comparisonGeneration.current;
    comparisonController.current?.abort();
    const controller = new AbortController();
    comparisonController.current = controller;
    const scope = comparisonScope;
    const session = authSessionSnapshot();
    const currentOperation = () => mounted.current && !controller.signal.aborted && generation === comparisonGeneration.current &&
      latestComparisonScope.current === scope && !latestChecking.current && isCurrentAuthSession(session);
    setLoadingComparison(true);
    setComparison(current => current ? { ...current, latestError: undefined } : null);
    try {
      const latest = await getDocument(vault, VAULT_SKILL_PATH, undefined, { preserveSession: true, signal: controller.signal });
      if (!currentOperation()) return;
      if (!validRevision(latest.current_commit)) throw new Error("The latest guide has no valid revision. Reload it before saving.");
      setGuideUnavailable(current => current?.operation === "write" ? current : null);
      setComparison(current => current ? {
        ...current,
        latest: guideSnapshot("Latest server version", latest.current_commit, latest.content || ""),
        latestError: undefined,
        observedCommit: remoteCommit,
        resolution: undefined,
      } : null);
    } catch (caught) {
      if (!currentOperation()) return;
      if (terminalFailure(caught)) setGuideUnavailable({ scope: ownerScope, verificationScope: scope, operation: "read", error: caught });
      setComparison(current => current ? {
        ...current, latest: null,
        latestError: "The latest guide could not be loaded. Your draft is preserved; try loading it again.",
      } : null);
    } finally {
      if (currentOperation()) setLoadingComparison(false);
      if (comparisonController.current === controller) comparisonController.current = null;
    }
  }

  async function openComparison() {
    if (!canManage || checking || latestChecking.current || busy || saving || unavailable) return;
    setComparison({
      scope: ownerScope,
      base: guideSnapshot("Original base", baseCommit, baseBody ?? ""),
      local: guideSnapshot("Your draft", baseCommit, draft),
      latest: null,
      observedCommit: remoteCommit,
    });
    await loadComparison();
  }

  function applyCombinedChanges(result: DocumentConflictResult) {
    const reviewed = activeComparison?.latest;
    if (!reviewed || !validRevision(reviewed.commit) || !canManage || checking || busy || saving || unavailable) return;
    // Advance the base only to the immutable snapshot actually displayed in
    // the comparison, even if polling discovered another revision meanwhile.
    setBaseBody(reviewed.body);
    setBaseCommit(reviewed.commit);
    setDraft(result.body);
    setComparedBase({ priorCommit: propCommit, commit: reviewed.commit });
    setSaveError("");
    setSaveConflict(false);
    comparisonGeneration.current += 1;
    comparisonController.current?.abort();
    comparisonController.current = null;
    setComparison(null);
    setLoadingComparison(false);
  }

  async function handleSave() {
    if (!canManage || checking || busy || saving || unavailable) return;
    if (changedElsewhere) {
      await openComparison();
      return;
    }
    if (!validRevision(baseCommit)) {
      setSaveError("This guide has no valid revision. Reload it before saving.");
      return;
    }
    const scope = ownerScope;
    const session = authSessionSnapshot();
    const currentOperation = () => mounted.current && latestScope.current === scope && isCurrentAuthSession(session);
    setSaving(true);
    setSaveError("");
    setSaveConflict(false);
    try {
      const saved = await updateDocument(vault, VAULT_SKILL_PATH, {
        content: draft,
        expected_commit: baseCommit,
      });
      if (!currentOperation()) return;
      queryClient.setQueryData(queryKey, {
        ...doc,
        content: draft,
        current_commit: saved.current_commit ?? saved.commit_hash ?? doc?.current_commit,
      });
      discardDraft();
      invalidate();
      setTab("preview");
      setExpanded(false);
      setSaved(true);
      if (sectionRef.current?.contains(document.activeElement)) restoreEditFocus();
    } catch (e: unknown) {
      if (!currentOperation()) return;
      const message = e instanceof Error ? e.message : "Save failed";
      if (e instanceof ApiError && e.status === 409 && (e.detail as { code?: unknown } | null)?.code === "conflict") {
        setSaveConflict(true);
        void openComparison();
      } else {
        if (terminalFailure(e)) {
          const writeDenied = e.status === 403;
          setGuideUnavailable({ scope, verificationScope: latestComparisonScope.current, operation: writeDenied ? "write" : "read", error: e });
          // Reuse the foreground proof flow: identity, then the parent's fresh
          // vault-role read. A successful Head alone cannot restore writing.
          if (writeDenied) window.dispatchEvent(new Event("akb:revalidate-access"));
        }
        setSaveError(message);
      }
    } finally {
      if (mounted.current) setSaving(false);
    }
  }

  // Reset rejects on failure; the ConfirmDialog catches it and renders the
  // error inside the open dialog (one place), so the section keeps no error
  // state of its own for this path.
  async function handleReset() {
    if (!canManage || checking || busy) return;
    const scope = ownerScope;
    const session = authSessionSnapshot();
    const currentOperation = () => mounted.current && latestScope.current === scope && isCurrentAuthSession(session);
    setSaving(true);
    try {
      const template = await getSkillTemplate();
      if (!currentOperation() || latestChecking.current) return;
      const content = template.replaceAll("{vault}", vault);
      await updateDocument(vault, VAULT_SKILL_PATH, {
        content,
        expected_commit: resetBaseCommit || undefined,
      });
      if (!currentOperation()) return;
      discardDraft();
      setTab("preview");
      setExpanded(false);
      setSaved(true);
      queryClient.invalidateQueries({ queryKey: ["document", vault, VAULT_SKILL_PATH] });
      queryClient.invalidateQueries({ queryKey: ["vault-skill-preview", vault] });
    } finally {
      if (mounted.current) setSaving(false);
    }
  }

  return (
    <SettingsSection ref={sectionRef} id="skill" title="Vault guide">
      <SettingRow label={<>
          Instructions
          {doc?.updated_at && <p className="mt-1 text-xs font-normal text-foreground-muted">Updated {timeAgo(doc.updated_at)}</p>}
        </>}>
          {error && !isMirror && (
            <Alert variant="destructive" title="Couldn't load the vault guide">
              <p>{error}</p>
              {onRetry && <Button variant="outline" size="sm" className="mt-2" onClick={onRetry}>Retry</Button>}
            </Alert>
          )}
          {isMirror ? (
            <p className="text-sm leading-relaxed text-foreground-muted">This mirror uses instructions from its external git repository.</p>
          ) : loading ? (
            <LoadingState label="Loading vault guide"><Skeleton className="h-16 w-full rounded-[var(--radius-sm)]" /></LoadingState>
          ) : !doc && error ? null : !doc ? (
            <Alert variant="warning">The vault guide is missing. It is restored automatically by the system backfill; contact an administrator if this persists.</Alert>
          ) : (
            <>
              {!expanded && (
                <div className="mb-3 rounded-[var(--radius-sm)] border border-border bg-background px-3 py-2.5">
                  <p className="line-clamp-2 text-sm leading-relaxed text-foreground-muted">
                    {guideExcerpt(doc.content) || "No instructions yet."}
                  </p>
                </div>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="ghost" size="sm" aria-expanded={expanded && tab === "preview"} aria-controls="guide-reading-view"
                  className="min-h-10 text-link sm:min-h-8"
                  onClick={() => {
                    if (expanded && tab === "preview") { setExpanded(false); return; }
                    selectTab("preview");
                  }}
                >{expanded && tab === "preview" ? "Close guide preview" : "Preview guide"}</Button>
                {canManage && (
                  <Button ref={editButtonRef} variant="outline" size="sm" className="min-h-10 sm:min-h-8"
                    disabled={busy || (expanded && tab === "edit")} onClick={() => selectTab("edit")}>Edit guide</Button>
                )}
                <DropdownMenu.Root modal={false}>
                  <DropdownMenu.Trigger asChild>
                    <Button variant="outline" size="icon" aria-label="Guide options" className="h-10 w-10 sm:h-8 sm:w-8" disabled={busy || saving}>
                      <MoreHorizontal className="h-4 w-4" aria-hidden />
                    </Button>
                  </DropdownMenu.Trigger>
                  <DropdownMenu.Portal>
                    <DropdownMenu.Content align="end" sideOffset={6} collisionPadding={12} aria-label="Guide options"
                      className="z-[var(--z-popover)] min-w-48 rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md">
                      <DropdownMenu.Item onSelect={() => selectTab("agent")} className="flex min-h-10 cursor-pointer items-center rounded-[var(--radius-sm)] px-3 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover">Agent view</DropdownMenu.Item>
                      {doc.current_commit && <DropdownMenu.Item asChild>
                        <Link
                          className="flex min-h-10 items-center gap-2 rounded-[var(--radius-sm)] px-3 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover"
                          to={`/vault/${encodeURIComponent(vault)}/doc/${encodeURIComponent(VAULT_SKILL_PATH)}?commit=${doc.current_commit}`}
                          onClick={event => {
                            if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                            if (!requestNavigation(`/vault/${encodeURIComponent(vault)}/doc/${encodeURIComponent(VAULT_SKILL_PATH)}?commit=${doc.current_commit}`)) event.preventDefault();
                          }}
                        ><History className="h-3.5 w-3.5" aria-hidden />History</Link>
                      </DropdownMenu.Item>}
                      {canManage && <>
                        <DropdownMenu.Separator className="my-1 h-px bg-border" />
                        <DropdownMenu.Item onSelect={() => { setResetBaseCommit(doc.current_commit || null); setResetOpen(true); }}
                          className="flex min-h-10 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] px-3 text-sm text-destructive outline-none data-[highlighted]:bg-destructive-soft">
                          <RotateCcw className="h-3.5 w-3.5" aria-hidden />Reset to template
                        </DropdownMenu.Item>
                      </>}
                    </DropdownMenu.Content>
                  </DropdownMenu.Portal>
                </DropdownMenu.Root>
                {dirty && <span role="status" className="text-xs text-foreground-muted">Unsaved guide changes</span>}
                {saved && !dirty && <span role="status" className="text-xs text-success">Guide saved</span>}
              </div>

              {expanded && tab === "preview" && (
                <div id="guide-reading-view" role="region" aria-label="Guide preview" className="mt-3 rounded-[var(--radius-sm)] border border-border bg-surface py-3">
                  <MarkdownRender markdown={doc.content || ""} assetContext={{ mode: "authenticated", vault, document: VAULT_SKILL_PATH, commit: doc.current_commit || undefined }} />
                </div>
              )}
              {expanded && tab === "agent" && (
                <div role="region" aria-label="Agent view" className="mt-3 space-y-3 rounded-[var(--radius-sm)] border border-border bg-background p-3">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-medium">Agent context</p>
                    <Button variant="ghost" size="sm" onClick={() => { setExpanded(false); setTab("preview"); }}>Close agent view</Button>
                  </div>
                  <AgentPreview vault={vault} />
                </div>
              )}
              {expanded && tab === "edit" && canManage && (
                <div className="mt-3">
                  <Textarea aria-label="Vault guide body" value={draft} onChange={event => setDraft(event.target.value)}
                    disabled={saving || busy} rows={16} placeholder="Instructions for agents using this vault."
                    className="resize-y font-mono text-sm leading-relaxed" spellCheck={false} />
                  {saveError && <Alert variant="destructive" className="mt-3">{saveError}</Alert>}
                  {(unavailable || changedElsewhere || observedHead.error) && <Alert variant={unavailable ? "destructive" : "warning"}
                    title={unavailable ? "Guide unavailable" : changedElsewhere ? "Guide changed elsewhere" : "Could not check for changes"}
                    role="status" aria-live="polite" className="mt-3">
                    <p>{unavailable
                      ? unavailable.status === 404 ? "This guide is no longer available. Your draft is preserved; saving is disabled."
                        : "Guide access could not be verified. Your draft is preserved; saving is disabled."
                      : changedElsewhere ? "Your draft and original revision are preserved. Compare changes before saving."
                        : "Your draft is preserved. Saving still checks its original version."}</p>
                    {changedElsewhere && !unavailable && <Button variant="outline" size="sm" className="mt-2 min-h-10 sm:min-h-8"
                      disabled={busy || saving} onClick={() => void openComparison()}>Compare changes</Button>}
                  </Alert>}
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <Button size="sm" className="min-h-10 sm:min-h-8" onClick={handleSave} loading={saving} disabled={!dirty || busy || checking || !!unavailable}>
                      {!saving && <Save className="h-3.5 w-3.5" aria-hidden />}{saving ? "Saving…" : "Save guide"}
                    </Button>
                    <Button variant="outline" size="sm" className="min-h-10 sm:min-h-8" disabled={saving || busy} onClick={() => {
                      if (dirty) setPendingExit({ kind: "cancel" });
                      else { discardDraft(); setTab("preview"); setExpanded(false); restoreEditFocus(); }
                    }}>Cancel</Button>
                    <Button variant="ghost" size="sm" className="min-h-10 sm:min-h-8" onClick={() => void observedHead.check()}
                      loading={observedHead.checking} disabled={saving || busy || checking}>Check for changes</Button>
                  </div>
                </div>
              )}
            </>
          )}
      </SettingRow>

      {activeComparison && !checking && canManage && expanded && tab === "edit" && <DocumentConflictNotice
        key={activeComparison.latest?.commit ?? "loading-guide-comparison"}
        base={activeComparison.base}
        local={activeComparison.local}
        latest={activeComparison.latest}
        latestError={activeComparison.latestError}
        loading={loadingComparison}
        accessError={unavailable ? "Guide access could not be verified." : undefined}
        checkingAccess={observedHead.checking}
        onCheckAccess={() => void observedHead.check()}
        resolution={activeComparison.resolution}
        onResolutionChange={(resolution) => setComparison(current => current ? { ...current, resolution } : null)}
        newerRevision={remoteCommit !== activeComparison.observedCommit ? remoteCommit : null}
        onApply={applyCombinedChanges}
        onRetryLatest={() => void loadComparison()}
        onCancel={() => {
          comparisonGeneration.current += 1;
          comparisonController.current?.abort();
          comparisonController.current = null;
          setComparison(null);
          setLoadingComparison(false);
        }}
      />}

      <ConfirmDialog
        open={!checking && pendingExit !== null}
        onOpenChange={(open) => { if (!open) setPendingExit(null); }}
        title="Discard unsaved guide changes?"
        description={saving ? "Wait for the guide save to finish before leaving." : "Your unsaved guide draft will be discarded."}
        confirmLabel="Discard changes"
        cancelLabel="Keep editing"
        variant="destructive"
        busy={saving}
        onConfirm={confirmExit}
      />

      <ConfirmDialog
        open={!checking && resetOpen}
        onOpenChange={setResetOpen}
        title="Reset to template?"
        description="Replace current content and any unsaved draft with the AKB-default template? Previous content stays in version history."
        confirmLabel="Reset"
        variant="destructive"
        onConfirm={handleReset}
      />
    </SettingsSection>
  );
}
