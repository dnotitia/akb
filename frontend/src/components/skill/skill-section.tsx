import { useState } from "react";
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { BookOpen, History, RotateCcw, Save } from "lucide-react";
import { AgentPreview } from "./agent-preview";
import { MarkdownRender } from "@/components/markdown-render";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { SkillBadge } from "@/components/ui/skill-badge";
import { Panel } from "@/components/ui/panel";
import { SettingsSectionHeader } from "@/components/ui/settings-section-header";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { getSkillTemplate, updateDocument } from "@/lib/api";
import { VAULT_SKILL_PATH } from "@/lib/skill";
import { timeAgo } from "@/lib/utils";

interface SkillDoc {
  content?: string;
  updated_at?: string;
  current_commit?: string | null;
}

interface Props {
  vault: string;
  /** The canonical doc from the settings page's skillQuery; absent = missing. */
  doc?: SkillDoc | null;
  loading?: boolean;
  /** External-git mirror vaults never carry a vault guide (backend excludes
   *  them from the seed and the reservation backfill). */
  isMirror?: boolean;
  /** Owner-authorized management capability. Read surfaces remain visible. */
  canManage: boolean;
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
  isMirror,
  canManage,
}: Props) {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState("preview");
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saveConflict, setSaveConflict] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [baseCommit, setBaseCommit] = useState<string | null>(null);
  const [resetBaseCommit, setResetBaseCommit] = useState<string | null>(null);

  function invalidate() {
    queryClient.invalidateQueries({
      queryKey: ["document", vault, VAULT_SKILL_PATH],
    });
    queryClient.invalidateQueries({ queryKey: ["vault-skill-preview", vault] });
  }

  // Seed the draft from the freshest doc every time Edit is entered, so a
  // background refetch (or an agent's write) is never silently overwritten by
  // a stale buffer left over from an earlier visit to the tab.
  function selectTab(next: string) {
    if (next === "edit") {
      setDraft(doc?.content || "");
      setBaseCommit(doc?.current_commit || null);
      setSaveError("");
      setSaveConflict(false);
    }
    setTab(next);
  }

  async function handleSave() {
    setSaving(true);
    setSaveError("");
    setSaveConflict(false);
    try {
      await updateDocument(vault, VAULT_SKILL_PATH, {
        content: draft,
        expected_commit: baseCommit || undefined,
      });
      invalidate();
      setTab("preview");
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : "Save failed";
      if (/^current_commit moved:/i.test(message)) {
        // Keep the local buffer and stale OCC pin intact: silently rebasing a
        // full-body save would overwrite the other editor's changes. Refresh
        // the read surfaces so History points at the newest commit instead.
        invalidate();
        setSaveConflict(true);
        setSaveError(
          "The guide changed in another session. Your draft is still here and was not saved. Compare it with History, then reopen Edit from the latest version before applying your changes.",
        );
      } else {
        setSaveError(message);
      }
    } finally {
      setSaving(false);
    }
  }

  // Reset rejects on failure; the ConfirmDialog catches it and renders the
  // error inside the open dialog (one place), so the section keeps no error
  // state of its own for this path.
  async function handleReset() {
    const template = await getSkillTemplate();
    const content = template.replaceAll("{vault}", vault);
    await updateDocument(vault, VAULT_SKILL_PATH, {
      content,
      expected_commit: resetBaseCommit || undefined,
    });
    queryClient.invalidateQueries({
      queryKey: ["document", vault, VAULT_SKILL_PATH],
    });
    queryClient.invalidateQueries({ queryKey: ["vault-skill-preview", vault] });
  }

  return (
    <section id="skill" aria-labelledby="skill-h" className="scroll-mt-6">
      <SettingsSectionHeader
        id="skill-h"
        icon={BookOpen}
        title="Vault guide"
        description="The operating instructions injected into every agent session for this vault."
        tone="guide"
      />

      <Panel variant="workspace" className="border-border-strong">
        <div className="p-4">
          {isMirror ? (
            <Alert variant="info">
              Read-only mirror vaults don't carry a vault guide. Edit the guide
              in the external git repository that owns this content instead.
            </Alert>
          ) : loading ? (
            <Skeleton className="h-40 w-full" />
          ) : !doc ? (
            <Alert variant="warning">
              The vault guide is missing. It is restored automatically by the
              system backfill; contact an administrator if this persists.
            </Alert>
          ) : (
            <>
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-md)] border border-border bg-surface-2 px-3 py-2.5">
                <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
                  <SkillBadge defined />
                  <span className="text-foreground-muted">
                    {doc.updated_at
                      ? `Updated ${timeAgo(doc.updated_at)}`
                      : VAULT_SKILL_PATH}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  {doc.current_commit && (
                    <Button asChild variant="ghost" size="sm">
                      <Link
                        to={`/vault/${vault}/doc/${encodeURIComponent(VAULT_SKILL_PATH)}?commit=${doc.current_commit}`}
                      >
                        <History className="h-3.5 w-3.5" aria-hidden />
                        History
                      </Link>
                    </Button>
                  )}
                  {canManage && (
                    <Button
                      variant="outline"
                      size="sm"
                      className="border-destructive text-destructive hover:bg-destructive-soft"
                      onClick={() => {
                        setResetBaseCommit(doc.current_commit || null);
                        setResetOpen(true);
                      }}
                    >
                      <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                      Reset to template
                    </Button>
                  )}
                </div>
              </div>

              <Tabs value={tab} onValueChange={selectTab}>
                <TabsList className="max-w-full overflow-x-auto">
                  <TabsTrigger value="preview">Preview</TabsTrigger>
                  <TabsTrigger value="agent">Agent view</TabsTrigger>
                  {canManage && <TabsTrigger value="edit">Edit</TabsTrigger>}
                </TabsList>

                <TabsContent value="preview">
                  <div className="min-w-0 rounded-[var(--radius-md)] border border-border bg-background p-5 sm:p-6">
                    <MarkdownRender
                      markdown={doc.content || ""}
                      assetContext={{
                        mode: "authenticated",
                        vault,
                        document: VAULT_SKILL_PATH,
                        commit: doc.current_commit || undefined,
                      }}
                    />
                  </div>
                </TabsContent>

                <TabsContent value="agent">
                  <p className="mb-3 max-w-prose text-xs leading-relaxed text-foreground-muted">
                    This is the server-composed payload an agent receives, not
                    merely the stored markdown document.
                  </p>
                  <AgentPreview vault={vault} />
                </TabsContent>

                {canManage && (
                  <TabsContent value="edit">
                    <Textarea
                      aria-label="Vault guide body"
                      value={draft}
                      onChange={(event) => setDraft(event.target.value)}
                      disabled={saving}
                      rows={20}
                      placeholder="Markdown body of the vault guide."
                      className="resize-y font-mono text-xs leading-relaxed"
                      spellCheck={false}
                    />
                    {saveError && (
                      <Alert
                        variant={saveConflict ? "warning" : "destructive"}
                        title={
                          saveConflict ? "Guide changed elsewhere" : undefined
                        }
                        className="mt-3"
                      >
                        {saveError}
                      </Alert>
                    )}
                    <div className="mt-3 flex items-center gap-3">
                      <Button
                        variant="accent"
                        onClick={handleSave}
                        loading={saving}
                        disabled={draft === (doc.content || "")}
                      >
                        {!saving && <Save className="h-4 w-4" aria-hidden />}
                        {saving ? "Saving…" : "Save guide"}
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() => selectTab("preview")}
                        disabled={saving}
                      >
                        Cancel
                      </Button>
                    </div>
                  </TabsContent>
                )}
              </Tabs>
            </>
          )}
        </div>
      </Panel>

      <ConfirmDialog
        open={resetOpen}
        onOpenChange={setResetOpen}
        title="Reset to template?"
        description="Replace current content with the AKB-default template? Previous content stays in git history."
        confirmLabel="Reset"
        variant="destructive"
        onConfirm={handleReset}
      />
    </section>
  );
}
