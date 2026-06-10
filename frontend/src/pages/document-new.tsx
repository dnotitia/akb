import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, ChevronRight } from "lucide-react";
import { ApiError, putDocument } from "@/lib/api";
import { DOC_TYPES, type DocType } from "@/lib/doc-constants";
import { useVaultTree, type TreeNode } from "@/hooks/use-vault-tree";
import { MarkdownEditorFallback } from "@/components/markdown-editor-fallback";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Eyebrow } from "@/components/ui/eyebrow";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { TagInput } from "@/components/ui/tag-input";
import { Textarea } from "@/components/ui/textarea";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

const MarkdownEditor = lazy(() => import("@/components/markdown-editor"));

/** Flatten every collection path in the tree (depth-first) for the
 *  COLLECTION datalist. The tree nests sub-collections under their
 *  parent, so a recursive walk yields the full `a/b/c` paths. */
function collectCollectionPaths(nodes: TreeNode[], out: string[] = []): string[] {
  for (const node of nodes) {
    if (node.kind === "collection") {
      out.push(node.path);
      if (node.children) collectCollectionPaths(node.children, out);
    }
  }
  return out;
}

export default function DocumentNewPage() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { tree } = useVaultTree(name);
  // Existing collections power the picker datalist; new names are still
  // accepted (the field stays a free-text input — "created automatically
  // if new").
  const collectionOptions = useMemo(
    () => Array.from(new Set(collectCollectionPaths(tree ?? []))).sort(),
    [tree],
  );
  const [title, setTitle] = useState("");
  // Prefill from `?collection=` so the tree's per-row "new doc" button
  // can drop the user straight into the owning collection.
  const [collection, setCollection] = useState(
    () => searchParams.get("collection") ?? "",
  );
  const [type, setType] = useState<DocType>("note");
  const [domain, setDomain] = useState("");
  const [summary, setSummary] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [body, setBody] = useState("");
  const [error, setError] = useState("");
  const [invalidField, setInvalidField] = useState<"title" | "collection" | "body" | null>(null);
  const [creating, setCreating] = useState(false);
  const [discardOpen, setDiscardOpen] = useState(false);
  const titleRef = useRef<HTMLInputElement>(null);
  const collectionRef = useRef<HTMLInputElement>(null);

  const isDirty =
    title.trim() !== "" ||
    collection.trim() !== "" ||
    domain.trim() !== "" ||
    summary.trim() !== "" ||
    tags.length > 0 ||
    body.trim() !== "";

  function doCancel() {
    if (typeof window !== "undefined" && window.history.length > 1) {
      navigate(-1);
    } else {
      navigate(`/vault/${name}`);
    }
  }
  function handleCancel() {
    // Guard a dirty draft behind a ConfirmDialog (design system bans window.confirm).
    if (isDirty && !creating) setDiscardOpen(true);
    else doCancel();
  }

  // Esc to cancel — off while saving and while the discard dialog is open
  // (Radix owns Esc there) so we never double-handle or discard mid-submit.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !creating && !discardOpen) handleCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creating, discardOpen, isDirty]);

  useEffect(() => {
    if (!isDirty || creating) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [isDirty, creating]);

  // Fail a field: surface the message, mark the field invalid (aria + red
  // border), and move focus to it so a keyboard/AT user lands on the problem.
  function fail(field: "title" | "collection" | "body", message: string) {
    setError(message);
    setInvalidField(field);
    if (field === "title") titleRef.current?.focus();
    else if (field === "collection") collectionRef.current?.focus();
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name) return;
    setError("");
    setInvalidField(null);
    const t = title.trim();
    const c = collection.trim();
    if (!t) return fail("title", "Title is required.");
    if (t.length > 256) return fail("title", "Title is too long (256 chars max).");
    if (!c) return fail("collection", "Collection is required.");
    // Allowlist: lowercase letters, digits, hyphens, underscores, and `/`
    // as a segment separator. Blocks path traversal (`..`), Windows-style
    // separators, absolute paths, and trailing slashes before the backend
    // ever sees the value. The backend should still validate, but a clear
    // client-side rejection produces a much better error message.
    if (!/^[a-z0-9_-]+(?:\/[a-z0-9_-]+)*$/.test(c)) {
      return fail(
        "collection",
        "Collection must use lowercase letters, digits, hyphens, underscores, and / only.",
      );
    }
    if (!body.trim()) return fail("body", "Body cannot be empty.");
    if (body.length > 1_000_000) return fail("body", "Body is too large (1 MB max).");
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
    } finally {
      // Always clear — a missing-path fallback or no-op navigate must not
      // leave the button stuck on "Creating…" with the form still mounted.
      setCreating(false);
    }
  }

  const canSubmit = title.trim() !== "" && collection.trim() !== "" && body.trim() !== "";

  return (
    <div className="max-w-3xl mx-auto fade-up">
      <nav aria-label="Breadcrumb" className="flex items-center gap-2 coord mb-6">
        <Link
          to={`/vault/${name}`}
          className="hover:text-link rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          {name || ""}
        </Link>
        <ChevronRight className="h-3 w-3 text-foreground-muted" aria-hidden />
        <span className="text-foreground">New document</span>
      </nav>

      <header className="pb-4">
        <Eyebrow tone="spark" className="mb-2 block">New document</Eyebrow>
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
        className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm p-8 space-y-5"
      >
        <div className="space-y-1.5">
          <Label htmlFor="doc-title">
            Title <span className="text-destructive normal-case">*</span>
          </Label>
          <Input
            id="doc-title"
            ref={titleRef}
            value={title}
            onChange={(e) => {
              setTitle(e.target.value);
              if (invalidField === "title") setInvalidField(null);
            }}
            placeholder="A short, descriptive title"
            maxLength={256}
            required
            aria-required="true"
            aria-invalid={invalidField === "title" || undefined}
            aria-describedby={error ? "doc-form-error" : undefined}
            autoFocus
          />
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label htmlFor="doc-collection">
              Collection <span className="text-destructive normal-case">*</span>
            </Label>
            <Input
              id="doc-collection"
              ref={collectionRef}
              value={collection}
              onChange={(e) => {
                setCollection(e.target.value);
                if (invalidField === "collection") setInvalidField(null);
              }}
              placeholder="e.g. engineering/specs"
              className="font-mono"
              maxLength={120}
              required
              aria-required="true"
              aria-invalid={invalidField === "collection" || undefined}
              list="doc-collection-options"
              autoComplete="off"
            />
            <datalist id="doc-collection-options">
              {collectionOptions.map((c) => (
                <option key={c} value={c} />
              ))}
            </datalist>
            <p className="text-xs text-foreground-muted">
              Pick an existing path or type a new one — it's created automatically.
            </p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="doc-type">Type</Label>
            <Select
              id="doc-type"
              value={type}
              onChange={(e) => setType(e.target.value as DocType)}
            >
              {DOC_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </Select>
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
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

        <div
          className={
            invalidField === "body"
              ? "space-y-1.5 rounded-[var(--radius-lg)] ring-2 ring-destructive ring-offset-2 ring-offset-background"
              : "space-y-1.5"
          }
        >
          <Label htmlFor="doc-body" id="doc-body-label">
            Body <span className="text-destructive normal-case">*</span>
          </Label>
          <Suspense fallback={<MarkdownEditorFallback />}>
            <MarkdownEditor
              value=""
              onChange={(md) => {
                setBody(md);
                if (invalidField === "body") setInvalidField(null);
              }}
              placeholder="Write the document body in markdown."
              ariaLabelledby="doc-body-label"
              required
            />
          </Suspense>
        </div>

        {error && (
          <Alert variant="destructive" id="doc-form-error">{error}</Alert>
        )}

        <div className="flex gap-3 pt-2">
          <Button type="submit" variant="accent" loading={creating} disabled={!canSubmit}>
            {!creating && (
              <>
                Create document
                <ArrowRight className="h-4 w-4" aria-hidden />
              </>
            )}
            {creating && "Creating…"}
          </Button>
          <Button type="button" variant="outline" onClick={handleCancel} disabled={creating}>
            <ArrowLeft className="h-4 w-4" aria-hidden /> Cancel
          </Button>
        </div>
      </form>

      <ConfirmDialog
        open={discardOpen}
        onOpenChange={setDiscardOpen}
        title="Discard this draft?"
        description="Your unsaved document will be lost."
        confirmLabel="Discard"
        variant="destructive"
        onConfirm={doCancel}
      />
    </div>
  );
}
