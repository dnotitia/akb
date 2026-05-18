import { useMemo, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Loader2 } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { getDocument } from "@/lib/api";
import { parseHeadings, slugify } from "@/lib/markdown";

interface DocumentViewProps {
  vault: string;
  docId: string;
  /** Controlled view — provide this + onViewChange to sync with URL params */
  view?: "rendered" | "raw";
  onViewChange?: (next: "rendered" | "raw") => void;
}

/**
 * Self-sufficient doc body: fetches the document, renders the
 * rendered/raw segmented control, and shows the markdown content.
 *
 * The query key is ["document", vault, docId] — identical to the key
 * used in DocumentPage, so TanStack Query dedupes the fetch when both
 * are mounted simultaneously.
 *
 * view/onViewChange are optional: when omitted, DocumentView manages
 * its own local toggle state (useful for VaultSkillPage and future
 * embeds). When provided, the caller drives view state (DocumentPage
 * uses this to sync ?view= URL params).
 *
 * T6 NOTE: The segmented control block below is where the AGENT
 * segment will land. When T6 adds it, extend the `ViewMode` union and
 * add a third tab here alongside the doc.type === "skill" guard.
 */
export function DocumentView({ vault, docId, view: viewProp, onViewChange }: DocumentViewProps) {
  const [localView, setLocalView] = useState<"rendered" | "raw">("rendered");

  // Controlled vs. uncontrolled view mode
  const view = viewProp ?? localView;
  const setView = (next: "rendered" | "raw") => {
    if (onViewChange) {
      onViewChange(next);
    } else {
      setLocalView(next);
    }
  };

  const { data: doc, isLoading, error } = useQuery({
    queryKey: ["document", vault, docId],
    queryFn: () => getDocument(vault, docId),
    enabled: !!vault && !!docId,
    retry: false,
  });

  const [copiedRaw, setCopiedRaw] = useState(false);

  async function copyRaw() {
    try {
      await navigator.clipboard.writeText(doc?.content || "");
      setCopiedRaw(true);
      setTimeout(() => setCopiedRaw(false), 1500);
    } catch {
      // clipboard API may be unavailable; silently no-op
    }
  }

  const markdownComponents = useMemo(
    () => buildHeadingComponents(doc?.content || ""),
    [doc?.content],
  );

  if (isLoading) {
    return (
      <div className="py-8 coord">
        <Loader2 className="h-4 w-4 inline animate-spin mr-2" aria-hidden />
        Loading…
      </div>
    );
  }

  if (error || !doc) {
    return null;
  }

  return (
    <>
      {/* ── Rendered/Raw segmented control ──────────────────────────
          T6: add AGENT tab here for skill docs (doc.type === "skill") */}
      <div className="flex items-center justify-end mb-3">
        <div
          role="tablist"
          aria-label="Document view"
          className="inline-flex border border-border"
        >
          <button
            role="tab"
            aria-selected={view === "rendered"}
            onClick={() => setView("rendered")}
            className={`px-2.5 py-1 text-[11px] font-mono uppercase tracking-wider transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background ${
              view === "rendered"
                ? "bg-foreground text-background"
                : "text-foreground-muted hover:text-foreground hover:bg-surface-muted"
            }`}
          >
            RENDERED
          </button>
          <button
            role="tab"
            aria-selected={view === "raw"}
            onClick={() => setView("raw")}
            className={`px-2.5 py-1 text-[11px] font-mono uppercase tracking-wider border-l border-border transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background ${
              view === "raw"
                ? "bg-foreground text-background"
                : "text-foreground-muted hover:text-foreground hover:bg-surface-muted"
            }`}
          >
            RAW
          </button>
        </div>
      </div>

      {/* ── Doc body ──────────────────────────────────────────────── */}
      {view === "rendered" ? (
        <div
          className="prose dark:prose-invert min-w-0"
          style={{ maxWidth: "100%" }}
        >
          <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {doc.content || ""}
          </Markdown>
        </div>
      ) : (
        <div className="relative">
          <button
            type="button"
            onClick={copyRaw}
            aria-label="Copy markdown"
            className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-1 text-[11px] font-mono uppercase tracking-wider text-foreground-muted hover:text-accent border border-border bg-surface transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            {copiedRaw ? "COPIED" : "COPY"}
          </button>
          <pre
            data-testid="doc-raw"
            className="font-mono text-[13px] leading-[1.55] whitespace-pre-wrap overflow-x-auto bg-surface-muted p-4 border border-border"
          >
            {doc.content || ""}
          </pre>
        </div>
      )}
    </>
  );
}

// ── Heading renderer helpers ─────────────────────────────────────

function buildHeadingComponents(markdown: string) {
  const slugQueue = parseHeadings(markdown).map((h) => h.slug);
  let cursor = 0;
  const make = (level: 1 | 2 | 3 | 4 | 5 | 6) => (props: any) => {
    const id = slugQueue[cursor++] ?? slugify(flattenText(props.children)) ?? `heading-${level}`;
    const Tag = `h${level}` as any;
    return <Tag id={id} {...props} />;
  };
  return {
    h1: make(1), h2: make(2), h3: make(3), h4: make(4), h5: make(5), h6: make(6),
  };
}

function flattenText(children: any): string {
  if (typeof children === "string") return children;
  if (Array.isArray(children)) return children.map(flattenText).join("");
  if (children?.props?.children) return flattenText(children.props.children);
  return "";
}
