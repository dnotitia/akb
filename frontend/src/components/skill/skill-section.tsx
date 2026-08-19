import { useState } from "react";
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { History, RotateCcw, Save } from "lucide-react";
import { AgentPreview } from "./agent-preview";
import { MarkdownRender } from "@/components/markdown-render";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { SkillBadge } from "@/components/ui/skill-badge";
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
export function SkillSection({ vault, doc, loading, isMirror }: Props) {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState("preview");
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [resetOpen, setResetOpen] = useState(false);

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["document", vault, VAULT_SKILL_PATH] });
    queryClient.invalidateQueries({ queryKey: ["vault-skill-preview", vault] });
  }

  // Seed the draft from the freshest doc every time Edit is entered, so a
  // background refetch (or an agent's write) is never silently overwritten by
  // a stale buffer left over from an earlier visit to the tab.
  function selectTab(next: string) {
    if (next === "edit") {
      setDraft(doc?.content || "");
      setSaveError("");
    }
    setTab(next);
  }

  async function handleSave() {
    setSaving(true);
    setSaveError("");
    try {
      await updateDocument(vault, VAULT_SKILL_PATH, { content: draft });
      invalidate();
      setTab("preview");
    } catch (e: any) {
      setSaveError(e?.message || "Save failed");
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
    await updateDocument(vault, VAULT_SKILL_PATH, { content });
    queryClient.invalidateQueries({ queryKey: ["document", vault, VAULT_SKILL_PATH] });
    queryClient.invalidateQueries({ queryKey: ["vault-skill-preview", vault] });
  }

  return (
    <section id="skill" aria-labelledby="skill-h" className="mb-12 scroll-mt-6">
      <header className="flex items-baseline gap-3 pb-3 border-b border-border mb-4">
        <h2 id="skill-h" className="coord-ink">
          Vault guide
        </h2>
        <span className="coord">agents read this first · system-managed</span>
      </header>

      {isMirror ? (
        <p className="text-sm text-foreground-muted leading-relaxed max-w-prose">
          Read-only mirror vaults don't carry a vault guide. Content here is
          synced from an external git repository; edit the guide in that
          repository instead.
        </p>
      ) : loading ? (
        <Skeleton className="h-40 w-full" />
      ) : !doc ? (
        <p className="text-sm text-foreground-muted leading-relaxed max-w-prose">
          The vault guide is missing. It is restored automatically by the system
          backfill; contact an administrator if this persists.
        </p>
      ) : (
        <>
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 mb-4">
            <div className="flex items-center gap-3 text-xs">
              <SkillBadge defined />
              <span className="text-foreground-muted">
                {doc.updated_at
                  ? `Last updated ${timeAgo(doc.updated_at)}`
                  : "overview/vault-skill.md"}
              </span>
            </div>
            <div className="flex items-center gap-1">
              {doc.current_commit && (
                <Button asChild variant="ghost" size="sm">
                  <Link
                    to={`/vault/${vault}/doc/${encodeURIComponent(VAULT_SKILL_PATH)}?commit=${doc.current_commit}`}
                  >
                    <History className="h-3 w-3" aria-hidden />
                    History
                  </Link>
                </Button>
              )}
              <Button variant="ghost" size="sm" onClick={() => setResetOpen(true)}>
                <RotateCcw className="h-3 w-3" aria-hidden />
                Reset to template
              </Button>
            </div>
          </div>

          <Tabs value={tab} onValueChange={selectTab}>
            <TabsList>
              <TabsTrigger value="preview">Preview</TabsTrigger>
              <TabsTrigger value="agent">Agent view</TabsTrigger>
              <TabsTrigger value="edit">Edit</TabsTrigger>
            </TabsList>

            <TabsContent value="preview">
              <div className="rounded-[var(--radius-lg)] border border-border bg-surface p-4 min-w-0">
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
              <p className="text-xs text-foreground-muted leading-relaxed max-w-prose mb-2">
                The guide exactly as an agent receives it — server-composed, not
                the stored markdown.
              </p>
              <AgentPreview vault={vault} />
            </TabsContent>

            <TabsContent value="edit">
              <Textarea
                aria-label="Vault guide body"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                disabled={saving}
                rows={20}
                placeholder="Markdown body of the vault guide."
                className="resize-y font-mono text-[12px] leading-relaxed"
                spellCheck={false}
              />
              {saveError && (
                <Alert variant="destructive" className="mt-3">
                  {saveError}
                </Alert>
              )}
              <div className="flex items-center gap-3 mt-3">
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
          </Tabs>
        </>
      )}

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
