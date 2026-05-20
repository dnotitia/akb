import { Suspense, lazy, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, ChevronRight, Loader2 } from "lucide-react";
import { ApiError, putDocument } from "@/lib/api";
import { DOC_TYPES, type DocType } from "@/lib/doc-constants";
import { MarkdownEditorFallback } from "@/components/markdown-editor-fallback";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TagInput } from "@/components/ui/tag-input";
import { Textarea } from "@/components/ui/textarea";

const MarkdownEditor = lazy(() => import("@/components/markdown-editor"));

export default function DocumentNewPage() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const [title, setTitle] = useState("");
  const [collection, setCollection] = useState("");
  const [type, setType] = useState<DocType>("note");
  const [domain, setDomain] = useState("");
  const [summary, setSummary] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [body, setBody] = useState("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);

  function handleCancel() {
    if (typeof window !== "undefined" && window.history.length > 1) {
      navigate(-1);
    } else {
      navigate(`/vault/${name}`);
    }
  }

  // Esc to cancel — mirrors vault-new.tsx, off while saving so we never
  // discard mid-submit.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !creating) handleCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creating]);

  const isDirty =
    title.trim() !== "" ||
    collection.trim() !== "" ||
    domain.trim() !== "" ||
    summary.trim() !== "" ||
    tags.length > 0 ||
    body.trim() !== "";

  useEffect(() => {
    if (!isDirty || creating) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [isDirty, creating]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name) return;
    const t = title.trim();
    const c = collection.trim();
    if (!t) {
      setError("Title is required.");
      return;
    }
    if (t.length > 256) {
      setError("Title is too long (256 chars max).");
      return;
    }
    if (!c) {
      setError("Collection is required.");
      return;
    }
    // Allowlist: lowercase letters, digits, hyphens, underscores, and `/`
    // as a segment separator. Blocks path traversal (`..`), Windows-style
    // separators, absolute paths, and trailing slashes before the backend
    // ever sees the value. The backend should still validate, but a clear
    // client-side rejection produces a much better error message.
    if (!/^[a-z0-9_-]+(?:\/[a-z0-9_-]+)*$/.test(c)) {
      setError(
        "Collection must use lowercase letters, digits, hyphens, underscores, and `/` only.",
      );
      return;
    }
    if (!body.trim()) {
      setError("Body cannot be empty.");
      return;
    }
    if (body.length > 1_000_000) {
      setError("Body is too large (1 MB max).");
      return;
    }
    setError("");
    setCreating(true);
    try {
      const result = await putDocument({
        vault: name,
        collection: c,
        title: t,
        content: body,
        type,
        tags,
        domain: domain.trim() || undefined,
        summary: summary.trim() || undefined,
      });
      const path = result?.path;
      if (path) {
        navigate(`/vault/${name}/doc/${encodeURIComponent(path)}`);
      } else {
        // Backend should always return path; fall back to vault root if not.
        navigate(`/vault/${name}`);
      }
    } catch (err: unknown) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to create document.";
      setError(message);
      setCreating(false);
    }
  }

  return (
    <div className="max-w-3xl mx-auto fade-up">
      <nav aria-label="Breadcrumb" className="flex items-center gap-2 coord mb-6">
        <Link to={`/vault/${name}`} className="hover:text-accent">
          {(name || "").toUpperCase()}
        </Link>
        <ChevronRight className="h-3 w-3 text-foreground-muted" aria-hidden />
        <span className="text-foreground">NEW DOCUMENT</span>
      </nav>

      <header className="border-b border-border pb-4 mb-0">
        <div className="coord-spark mb-2">§ NEW DOCUMENT</div>
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">
          New document.
        </h1>
        <p className="mt-3 text-sm text-foreground-muted max-w-prose">
          A document is a markdown file committed to the vault repo. Pick the
          collection it lives under (created automatically if new) and write
          the body in markdown.
        </p>
      </header>

      <form
        onSubmit={handleSubmit}
        className="border border-border border-t-0 bg-surface p-8 space-y-5"
      >
        <div className="space-y-1.5">
          <Label htmlFor="doc-title">
            Title <span className="text-destructive normal-case">*</span>
          </Label>
          <Input
            id="doc-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="A short, descriptive title"
            maxLength={256}
            autoFocus
          />
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label htmlFor="doc-collection">
              Collection <span className="text-destructive normal-case">*</span>
            </Label>
            <Input
              id="doc-collection"
              value={collection}
              onChange={(e) => setCollection(e.target.value)}
              placeholder="e.g. engineering/specs"
              className="font-mono"
              maxLength={120}
            />
            <div className="coord">CREATED AUTOMATICALLY IF NEW</div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="doc-type">Type</Label>
            <select
              id="doc-type"
              value={type}
              onChange={(e) => setType(e.target.value as DocType)}
              className="w-full h-9 px-3 bg-surface border border-border text-sm text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background cursor-pointer"
            >
              {DOC_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label htmlFor="doc-domain">
              Domain{" "}
              <span className="normal-case tracking-normal text-foreground-muted">
                (optional)
              </span>
            </Label>
            <Input
              id="doc-domain"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              placeholder="engineering, product, ops, …"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="doc-tags">
              Tags{" "}
              <span className="normal-case tracking-normal text-foreground-muted">
                (optional)
              </span>
            </Label>
            <TagInput id="doc-tags" value={tags} onChange={setTags} />
          </div>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="doc-summary">
            Summary{" "}
            <span className="normal-case tracking-normal text-foreground-muted">
              (optional — auto-generated from body if blank)
            </span>
          </Label>
          <Textarea
            id="doc-summary"
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            rows={2}
            placeholder="One-line description used in search results."
            className="resize-y"
            maxLength={500}
          />
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="doc-body">
            Body <span className="text-destructive normal-case">*</span>
          </Label>
          <Suspense fallback={<MarkdownEditorFallback />}>
            <MarkdownEditor
              value=""
              onChange={setBody}
              placeholder="Write the document body in markdown."
            />
          </Suspense>
        </div>

        {error && (
          <div
            role="alert"
            aria-live="polite"
            className="border border-destructive px-3 py-2 text-xs font-mono uppercase tracking-wider text-destructive"
          >
            ⚠ {error.toUpperCase()}
          </div>
        )}

        <div className="flex gap-3 pt-2">
          <Button type="submit" variant="accent" disabled={creating}>
            {creating ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Creating…
              </>
            ) : (
              <>
                Create document
                <ArrowRight className="h-4 w-4" aria-hidden />
              </>
            )}
          </Button>
          <Button type="button" variant="outline" onClick={handleCancel} disabled={creating}>
            <ArrowLeft className="h-4 w-4" aria-hidden /> Cancel
          </Button>
        </div>
      </form>
    </div>
  );
}
