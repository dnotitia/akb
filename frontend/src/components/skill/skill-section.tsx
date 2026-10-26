import { lazy, Suspense, useCallback, useEffect, useId, useRef, useState } from "react";
import { Link, useLocation, useNavigate, type NavigateOptions } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { BookOpen, History, MoreHorizontal, RotateCcw, Save } from "lucide-react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { AgentPreview } from "./agent-preview";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadingState } from "@/components/ui/loading-state";
import { Textarea } from "@/components/ui/textarea";
import { TonalIcon } from "@/components/ui/tonal-icon";
import { SettingsSection } from "@/components/ui/settings-section";
import { authSessionSnapshot, getSkillTemplate, isCurrentAuthSession, updateDocument } from "@/lib/api";
import { VAULT_SKILL_PATH } from "@/lib/skill";
import { documentPreviewState } from "@/lib/document-preview-navigation";
import { retainsGuideSettings, vaultGuideHref } from "@/lib/vault-guide-navigation";
import { timeAgo } from "@/lib/utils";
import { useResourceNavigation, useResourceNavigationGuard } from "@/contexts/resource-navigation-context";
import { useDocumentHistoryGuard } from "@/hooks/use-document-history-guard";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";

interface SkillDoc {
  content?: string;
  updated_at?: string;
  current_commit?: string | null;
}

const GuidePreview = lazy(() => import("./guide-preview"));

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
 * The only guide editing surface. Reading uses the shared route-backed reader;
 * body editing, agent context, reset and history management live here.
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
  const { checking } = useAccessVerification();
  const user = useCurrentUser();
  const ownerScope = JSON.stringify([vault, user?.user_id]);
  const latestScope = useRef(ownerScope);
  latestScope.current = ownerScope;
  const latestChecking = useRef(checking);
  latestChecking.current = checking;
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const consumedEditRequest = useRef<string | null>(null);
  const previewId = useId();
  const previewHref = vaultGuideHref(vault);
  const previewState = documentPreviewState(location, previewId);
  const { requestNavigation } = useResourceNavigation();
  const [tab, setTab] = useState("preview");
  const [expanded, setExpanded] = useState(false);
  const sectionRef = useRef<HTMLElement>(null);
  const editButtonRef = useRef<HTMLButtonElement>(null);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saveConflict, setSaveConflict] = useState(false);
  const [saved, setSaved] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [baseCommit, setBaseCommit] = useState<string | null>(null);
  const [baseBody, setBaseBody] = useState<string | null>(null);
  const [resetBaseCommit, setResetBaseCommit] = useState<string | null>(null);
  const [pendingExit, setPendingExit] = useState<
    { kind: "cancel" } | { kind: "navigation"; href: string; options?: NavigateOptions } | { kind: "history"; proceed: () => void } | null
  >(null);
  const dirty = baseBody !== null && draft !== baseBody;
  useEffect(() => { onDraftStateChange?.({ dirty, saving }); }, [dirty, saving, onDraftStateChange]);
  const changedElsewhere = dirty && baseCommit !== (doc?.current_commit || null);
  const guardNavigation = useCallback((href: string, options?: NavigateOptions) => {
    // Settings anchors retain this mounted editor and its draft.
    if (new URL(href, window.location.origin).pathname === location.pathname) return true;
    if (retainsGuideSettings(vault, location, href, options?.state)) return true;
    setPendingExit({ kind: "navigation", href, options });
    return false;
  }, [location, vault]);
  const continueNavigation = useResourceNavigationGuard(!navigationManaged && (dirty || saving) ? guardNavigation : null);
  useDocumentHistoryGuard(!navigationManaged && (dirty || saving),
    (proceed) => setPendingExit({ kind: "history", proceed }),
    destination => destination.pathname !== location.pathname &&
      !retainsGuideSettings(vault, location, destination.href, window.history.state?.usr));
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
      setSaved(false);
    }
    setTab(next);
    setExpanded(true);
  }

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    if (params.get("edit") !== "guide" || location.hash !== "#skill" ||
        !canManage || checking || loading || busy || !doc) return;
    const request = JSON.stringify([ownerScope, location.key, location.pathname, location.search, location.hash]);
    if (consumedEditRequest.current === request) return;
    // History replacement can precede the router's location-context commit.
    // Claim this entry before updating the draft so Cancel cannot replay it.
    consumedEditRequest.current = request;
    // Consume the explicit edit request once. Returning from a reading preview
    // resumes an existing draft and its OCC pin instead of reseeding it.
    if (baseBody === null) {
      setDraft(doc.content || "");
      setBaseBody(doc.content || "");
      setBaseCommit(doc.current_commit || null);
      setSaveError("");
      setSaveConflict(false);
      setSaved(false);
    }
    setTab("edit");
    setExpanded(true);
    params.delete("edit");
    navigate({ pathname: location.pathname, search: params.toString(), hash: "#skill" }, { replace: true });
    const scope = ownerScope;
    requestAnimationFrame(() => {
      if (mounted.current && latestScope.current === scope) editorRef.current?.focus({ preventScroll: true });
    });
  }, [location, canManage, checking, loading, busy, doc, baseBody, navigate, ownerScope]);

  function discardDraft() {
    setDraft("");
    setBaseBody(null);
    setBaseCommit(null);
    setSaveError("");
    setSaveConflict(false);
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

  async function handleSave() {
    if (!canManage || checking || busy) return;
    const scope = ownerScope;
    const session = authSessionSnapshot();
    const currentOperation = () => mounted.current && latestScope.current === scope && isCurrentAuthSession(session);
    setSaving(true);
    setSaveError("");
    setSaveConflict(false);
    try {
      const saved = await updateDocument(vault, VAULT_SKILL_PATH, {
        content: draft,
        expected_commit: baseCommit || undefined,
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
      if (/^current_commit moved:/i.test(message)) {
        // Keep the local buffer and stale OCC pin intact: silently rebasing a
        // full-body save would overwrite the other editor's changes. Refresh
        // the read surfaces so History points at the newest commit instead.
        invalidate();
        setSaveConflict(true);
        setSaveError(
          "The guide changed in another session. Your draft is still here and was not saved. Copy it and compare with History. Cancel and discard only when you are ready to start from the latest version.",
        );
      } else {
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
      <div className="p-4 sm:p-5">
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
              <div className="flex min-w-0 flex-col gap-3 @[48rem]/settings-section:flex-row @[48rem]/settings-section:items-start">
                <div className="flex min-w-0 flex-1 items-start gap-3">
                  <TonalIcon tone="guide" size="sm"><BookOpen aria-hidden /></TonalIcon>
                  <div className="min-w-0 space-y-1">
                    {expanded && <p className="text-sm font-medium text-foreground">
                      {tab === "edit" ? "Editing guide" : "Agent view"}
                    </p>}
                    <div className="flex min-h-8 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-foreground-muted">
                      {doc.updated_at && <time dateTime={doc.updated_at}>Updated {timeAgo(doc.updated_at)}</time>}
                      {dirty && <span role="status">Unsaved guide changes</span>}
                      {saved && !dirty && <span role="status" className="text-success">Guide saved</span>}
                    </div>
                  </div>
                </div>
              <div className="flex flex-wrap items-center gap-2 pl-11 @[48rem]/settings-section:shrink-0 @[48rem]/settings-section:pl-0">
                <Button asChild variant="ghost" size="sm" className="min-h-11 text-link sm:min-h-8">
                  <Link className="workspace-control" id={previewId} to={previewHref} state={previewState} onClick={event => {
                    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                    if (!requestNavigation(previewHref, { state: previewState })) event.preventDefault();
                  }}>Open guide</Link>
                </Button>
                {canManage && (
                  <Button ref={editButtonRef} variant="outline" size="sm" className="min-h-11 sm:min-h-8"
                    disabled={busy || (expanded && tab === "edit")} onClick={() => selectTab("edit")}>Edit guide</Button>
                )}
                <DropdownMenu.Root modal={false}>
                  <DropdownMenu.Trigger asChild>
                    <Button variant="ghost" size="icon" aria-label="Guide options" className="workspace-icon-control h-11 w-11 sm:h-8 sm:w-8" disabled={busy || saving}>
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
              </div>
              </div>

              {!expanded && (doc.content?.trim() ? (
                <Suspense fallback={<LoadingState label="Loading guide preview"><Skeleton className="mt-4 h-32 w-full" /></LoadingState>}>
                  <GuidePreview vault={vault} content={doc.content} onNavigate={(event, url) => {
                    if (url.origin !== window.location.origin || url.pathname.startsWith("/api/")) return;
                    event.preventDefault();
                    const href = `${url.pathname}${url.search}${url.hash}`;
                    const options = url.pathname === previewHref ? { state: previewState } : undefined;
                    if (requestNavigation(href, options)) navigate(href, options);
                  }} />
                </Suspense>
              ) : <p className="mt-4 text-sm text-foreground-muted">No instructions yet.</p>)}

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
                  <Textarea ref={editorRef} autoFocus aria-label="Vault guide body" value={draft} onChange={event => setDraft(event.target.value)}
                    disabled={saving || busy} rows={16} placeholder="Instructions for agents using this vault."
                    className="resize-y font-mono text-sm leading-relaxed" spellCheck={false} />
                  {(saveError || changedElsewhere) && <Alert
                    variant={saveConflict || changedElsewhere ? "warning" : "destructive"}
                    title={saveConflict || changedElsewhere ? "Guide changed elsewhere" : undefined} className="mt-3">
                    {saveError || "The saved guide changed while you were editing. Your draft and original revision are preserved; compare with History before saving."}
                  </Alert>}
                  <div className="mt-3 flex items-center gap-2">
                    <Button size="sm" className="min-h-10 sm:min-h-8" onClick={handleSave} loading={saving} disabled={!dirty || busy}>
                      {!saving && <Save className="h-3.5 w-3.5" aria-hidden />}{saving ? "Saving…" : "Save guide"}
                    </Button>
                    <Button variant="outline" size="sm" className="min-h-10 sm:min-h-8" disabled={saving || busy} onClick={() => {
                      if (dirty) setPendingExit({ kind: "cancel" });
                      else { discardDraft(); setTab("preview"); setExpanded(false); restoreEditFocus(); }
                    }}>Cancel</Button>
                  </div>
                </div>
              )}
            </>
          )}
      </div>

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
