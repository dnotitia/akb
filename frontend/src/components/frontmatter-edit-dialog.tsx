import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { updateDocument } from "@/lib/api";
import { DOC_STATUSES, DOC_TYPES, type DocStatus, type DocType } from "@/lib/doc-constants";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
  const [title, setTitle] = useState("");
  const [type, setType] = useState<DocType>("note");
  const [status, setStatus] = useState<DocStatus>("draft");
  const [domain, setDomain] = useState("");
  const [summary, setSummary] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [content, setContent] = useState("");
  const [initialContent, setInitialContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // Hydrate from doc on every open so the form always reflects the latest
  // server state — guards against stale edits when the user reopens after
  // the doc was mutated by an agent.
  useEffect(() => {
    if (!open) return;
    setTitle(doc.title || "");
    setType((DOC_TYPES.includes(doc.type as DocType) ? doc.type : "note") as DocType);
    setStatus(
      (DOC_STATUSES.includes(doc.status as DocStatus) ? doc.status : "draft") as DocStatus,
    );
    setDomain(doc.domain || "");
    setSummary(doc.summary || "");
    setTags(doc.tags || []);
    setContent(doc.content || "");
    setInitialContent(doc.content || "");
    setError("");
  }, [open, doc]);

  async function handleSave() {
    if (!title.trim()) {
      setError("Title cannot be empty.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      // Only include `content` in the PATCH when body editing is enabled AND
      // the body actually changed — otherwise updateDocument with content
      // would touch git and the doc's chunks unnecessarily.
      const contentChanged = editBody && content !== initialContent;
      const payload: Record<string, unknown> = {
        title: title.trim(),
        type,
        status,
        domain: domain.trim() || null,
        summary: summary.trim() || null,
        tags,
      };
      if (contentChanged) payload.content = content;
      const result = await updateDocument(vault, docId, payload);
      onSaved({
        ...doc,
        title: title.trim(),
        type,
        status,
        domain: domain.trim() || undefined,
        summary: summary.trim() || undefined,
        tags,
        content: contentChanged ? content : doc.content,
        path: result?.path || doc.path,
      });
      onOpenChange(false);
    } catch (e: any) {
      setError(e?.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !saving && onOpenChange(o)}>
      <DialogContent
        className={`${editBody ? "max-w-3xl" : "max-w-2xl"} max-h-[90vh] flex flex-col`}
      >
        <DialogHeader>
          <DialogTitle>Edit details</DialogTitle>
          <DialogDescription>
            {editBody
              ? "Update the document's metadata and body."
              : "Update the document's metadata. The body stays as-is."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 flex-1 overflow-y-auto pr-1 -mr-1">
          <div>
            <Label htmlFor="fm-title" className="coord-ink mb-1.5 block">
              TITLE
            </Label>
            <Input
              id="fm-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              autoFocus
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <Label htmlFor="fm-type" className="coord-ink mb-1.5 block">
                TYPE
              </Label>
              <select
                id="fm-type"
                value={type}
                onChange={(e) => setType(e.target.value as DocType)}
                className="w-full h-10 px-3 bg-surface border border-border text-sm text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background cursor-pointer"
              >
                {DOC_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <Label htmlFor="fm-status" className="coord-ink mb-1.5 block">
                STATUS
              </Label>
              <select
                id="fm-status"
                value={status}
                onChange={(e) => setStatus(e.target.value as DocStatus)}
                className="w-full h-10 px-3 bg-surface border border-border text-sm text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background cursor-pointer"
              >
                {DOC_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
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
                BODY
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

          {error && (
            <div role="alert" className="border border-destructive p-2 text-xs text-destructive">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={saving}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="accent"
            onClick={handleSave}
            disabled={saving || !title.trim()}
          >
            {saving ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Saving…
              </>
            ) : (
              "Save"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
