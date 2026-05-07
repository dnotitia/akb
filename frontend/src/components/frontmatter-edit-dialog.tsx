import { useEffect, useState } from "react";
import { Loader2, X } from "lucide-react";
import { updateDocument } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const DOC_TYPES = [
  "note",
  "report",
  "decision",
  "spec",
  "plan",
  "session",
  "task",
  "reference",
] as const;

const DOC_STATUSES = ["draft", "active", "archived", "superseded"] as const;

type DocType = (typeof DOC_TYPES)[number];
type DocStatus = (typeof DOC_STATUSES)[number];

interface DocLike {
  id?: string;
  path?: string;
  title?: string;
  type?: string;
  status?: string;
  domain?: string;
  summary?: string;
  tags?: string[];
}

interface FrontmatterEditDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  docId: string;
  doc: DocLike;
  /** Called with the merged doc after a successful save. */
  onSaved: (next: DocLike) => void;
}

export function FrontmatterEditDialog({
  open,
  onOpenChange,
  vault,
  docId,
  doc,
  onSaved,
}: FrontmatterEditDialogProps) {
  const [title, setTitle] = useState("");
  const [type, setType] = useState<DocType>("note");
  const [status, setStatus] = useState<DocStatus>("draft");
  const [domain, setDomain] = useState("");
  const [summary, setSummary] = useState("");
  const [tagInput, setTagInput] = useState("");
  const [tags, setTags] = useState<string[]>([]);
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
    setTagInput("");
    setError("");
  }, [open, doc]);

  function commitTagInput() {
    const v = tagInput.trim().replace(/^#/, "");
    if (!v) return;
    if (!tags.includes(v)) setTags([...tags, v]);
    setTagInput("");
  }

  function removeTag(t: string) {
    setTags(tags.filter((x) => x !== t));
  }

  async function handleSave() {
    if (!title.trim()) {
      setError("Title cannot be empty.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const result = await updateDocument(vault, docId, {
        title: title.trim(),
        type,
        status,
        domain: domain.trim() || null,
        summary: summary.trim() || null,
        tags,
      });
      onSaved({
        ...doc,
        title: title.trim(),
        type,
        status,
        domain: domain.trim() || undefined,
        summary: summary.trim() || undefined,
        tags,
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
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Edit details</DialogTitle>
          <DialogDescription>
            Update the document's metadata. The body stays as-is.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
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
            {tags.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mb-2">
                {tags.map((t) => (
                  <span
                    key={t}
                    className="inline-flex items-center gap-1 px-2 py-0.5 border border-border bg-surface-muted text-xs font-mono"
                  >
                    #{t}
                    <button
                      type="button"
                      onClick={() => removeTag(t)}
                      aria-label={`Remove tag ${t}`}
                      className="text-foreground-muted hover:text-destructive cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    >
                      <X className="h-3 w-3" aria-hidden />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <Input
              id="fm-tag-input"
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === ",") {
                  e.preventDefault();
                  commitTagInput();
                } else if (e.key === "Backspace" && !tagInput && tags.length > 0) {
                  setTags(tags.slice(0, -1));
                }
              }}
              onBlur={commitTagInput}
              placeholder="Add tag and press Enter or comma"
            />
          </div>

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
