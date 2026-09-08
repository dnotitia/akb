import { useEffect, useRef, useState } from "react";
import { updateDocument } from "@/lib/api";
import { DOC_STATUSES, DOC_TYPES, type DocStatus, type DocType } from "@/lib/doc-constants";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SelectMenu } from "@/components/ui/select-menu";
import { TagInput } from "@/components/ui/tag-input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface DocLike {
  id?: string;
  path?: string;
  title?: string;
  type?: string;
  status?: string;
  current_commit?: string;
  domain?: string;
  summary?: string;
  tags?: string[];
  content?: string;
}

interface FrontmatterEditDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  docId: string;
  doc: DocLike;
  /** Called with the merged doc after a successful save. */
  onSaved: (next: DocLike) => void;
  /**
   * Show a body editor in addition to the frontmatter fields. Used by the
   * skill page where the doc is small and editing the body inline is the
   * primary edit affordance. Other doc pages keep body editing out of this
   * dialog so the description "metadata only" stays accurate there.
   */
  editBody?: boolean;
}

export function FrontmatterEditDialog({
  open,
  onOpenChange,
  vault,
  docId,
  doc,
  onSaved,
  editBody = false,
}: FrontmatterEditDialogProps) {
  const [type, setType] = useState<DocType>("note");
  const [status, setStatus] = useState<DocStatus>("draft");
  const [domain, setDomain] = useState("");
  const [summary, setSummary] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [content, setContent] = useState("");
  const [initialContent, setInitialContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [discardOpen, setDiscardOpen] = useState(false);
  // Snapshot of the hydrated values, to detect unsaved edits on close.
  const initialRef = useRef({ type: "note", status: "draft", domain: "", summary: "", tags: "", content: "" });

  // Hydrate from doc on every open so the form always reflects the latest
  // server state — guards against stale edits when the user reopens after
  // the doc was mutated by an agent.
  useEffect(() => {
    if (!open) return;
    const t = (DOC_TYPES.includes(doc.type as DocType) ? doc.type : "note") as DocType;
    const s = (DOC_STATUSES.includes(doc.status as DocStatus) ? doc.status : "draft") as DocStatus;
    setType(t);
    setStatus(s);
    setDomain(doc.domain || "");
    setSummary(doc.summary || "");
    setTags(doc.tags || []);
    setContent(doc.content || "");
    setInitialContent(doc.content || "");
    setError("");
    initialRef.current = {
      type: t,
      status: s,
      domain: doc.domain || "",
      summary: doc.summary || "",
      tags: (doc.tags || []).join("\0"),
      content: doc.content || "",
    };
  }, [open, doc]);

  const i = initialRef.current;
  const isDirty =
    type !== i.type ||
    status !== i.status ||
    domain !== i.domain ||
    summary !== i.summary ||
    tags.join("\0") !== i.tags ||
    (editBody && content !== i.content);

  // Close requests (Cancel, overlay, Esc, X) route through here so a dirty
  // form prompts before discarding (design system bans window.confirm).
  function requestClose() {
    if (saving) return;
    if (isDirty) setDiscardOpen(true);
    else onOpenChange(false);
  }

  async function handleSave() {
    setSaving(true);
    setError("");
    try {
      // Only include `content` in the PATCH when body editing is enabled AND
      // the body actually changed — otherwise updateDocument with content
      // would touch git and the doc's chunks unnecessarily.
      const contentChanged = editBody && content !== initialContent;
      const payload: Record<string, unknown> = {
        type,
        status,
        domain: domain.trim() || null,
        summary: summary.trim() || null,
        tags,
      };
      if (contentChanged) payload.content = content;
      if (doc.current_commit) payload.expected_commit = doc.current_commit;
      const result = await updateDocument(vault, docId, payload);
      onSaved({
        ...doc,
        type,
        status,
        domain: domain.trim() || undefined,
        summary: summary.trim() || undefined,
        tags,
        content: contentChanged ? content : doc.content,
        path: result?.path || doc.path,
        current_commit: result?.current_commit ?? result?.commit_hash ?? doc.current_commit,
      });
      onOpenChange(false);
    } catch (e: any) {
      setError(e?.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
    <Dialog open={open} onOpenChange={(o) => { if (!o) requestClose(); }}>
      <DialogContent
        onEscapeKeyDown={(e) => { e.preventDefault(); requestClose(); }}
        onInteractOutside={(e) => { e.preventDefault(); requestClose(); }}
        className={`${editBody ? "max-w-3xl" : "max-w-2xl"} max-h-[90vh] flex flex-col`}
      >
        <DialogHeader>
          <DialogTitle>Edit details</DialogTitle>
          <DialogDescription>
            {editBody
              ? "Update document metadata and body. Edit the title from the document editor."
              : "Update document metadata. The title and body stay as-is."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 flex-1 overflow-y-auto pr-1 -mr-1">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <Label htmlFor="fm-type" className="coord-ink mb-1.5 block">
                TYPE
              </Label>
              <SelectMenu
                id="fm-type"
                aria-label="Document type"
                value={type}
                onValueChange={(v) => setType(v as DocType)}
                options={DOC_TYPES.map((t) => ({ value: t, label: t }))}
              />
            </div>
            <div>
              <Label htmlFor="fm-status" className="coord-ink mb-1.5 block">
                STATUS
              </Label>
              <SelectMenu
                id="fm-status"
                aria-label="Document status"
                value={status}
                onValueChange={(v) => setStatus(v as DocStatus)}
                options={DOC_STATUSES.map((s) => ({ value: s, label: s }))}
              />
            </div>
          </div>

          <div>
            <Label htmlFor="fm-domain" className="coord-ink mb-1.5 block">
              DOMAIN
            </Label>
            <Input
              id="fm-domain"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              placeholder="engineering, product, ops, …"
            />
          </div>

          <div>
            <Label htmlFor="fm-summary" className="coord-ink mb-1.5 block">
              SUMMARY
            </Label>
            <Textarea
              id="fm-summary"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              rows={3}
              placeholder="Brief summary used in search and previews."
              className="resize-y"
            />
          </div>

          <div>
            <Label htmlFor="fm-tag-input" className="coord-ink mb-1.5 block">
              TAGS
            </Label>
            <TagInput id="fm-tag-input" value={tags} onChange={setTags} />
          </div>

          {editBody && (
            <div>
              <Label htmlFor="fm-content" className="coord-ink mb-1.5 block">
                BODY <span className="normal-case tracking-normal text-foreground-muted">(raw markdown)</span>
              </Label>
              <Textarea
                id="fm-content"
                value={content}
                onChange={(e) => setContent(e.target.value)}
                rows={18}
                placeholder="Markdown body of the document."
                className="resize-y font-mono text-[12px] leading-relaxed"
                spellCheck={false}
              />
            </div>
          )}

          {error && <Alert variant="destructive">{error}</Alert>}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={requestClose}
            disabled={saving}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="accent"
            onClick={handleSave}
            loading={saving}
            disabled={!isDirty}
          >
            {saving ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <ConfirmDialog
      open={discardOpen}
      onOpenChange={setDiscardOpen}
      title="Discard changes?"
      description="Your unsaved edits to this document's details will be lost."
      confirmLabel="Discard"
      variant="destructive"
      onConfirm={() => {
        setDiscardOpen(false);
        onOpenChange(false);
      }}
    />
    </>
  );
}
