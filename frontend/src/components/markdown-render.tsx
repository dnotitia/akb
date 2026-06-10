/**
 * MarkdownRender — the shared, hand-tuned markdown renderer for AKB
 * read views (DocumentView "Rendered" tab + public publication body).
 *
 * Tone reference: Notion / Linear / Coda — soft, rounded, editorial,
 * NOT IDE / dev-doc. The styling approach is adapted from
 * seahorse-mcp-agent-server's chat renderer, re-mapped onto AKB's
 * design tokens (var(--color-*) + bg-surface / bg-surface-2 /
 * border-border / text-accent / text-primary). NO hardcoded hex —
 * scripts/design-check.mjs fails the build on 6-digit hex.
 *
 * What we deliberately do here:
 *   • Code fences → soft rounded card with a chrome bar (language
 *     label + copy button), monochrome body. We DROP rehype-highlight
 *     and stay monochrome: syntax coloring would (a) require hljs hex
 *     theme colors the design-check forbids and (b) shift the tone to
 *     "developer tool." Matches seahorse's choice.
 *   • Inline code → small brand-tinted chip.
 *   • Blockquotes with `> [!NOTE|TIP|IMPORTANT|WARNING|CAUTION]` →
 *     Notion-style soft callout cards with a leading icon tile.
 *   • Tables → rounded overflow card, tinted thead, zebra + hover.
 *   • Headings → tuned cascade + stable `id`s consumed in document
 *     order from parseHeadings() so the outline scroll-sync keeps
 *     matching `#slug` anchors.
 *   • Links → run through sanitizeLinkUrl (strips javascript:/data:/
 *     vbscript:/protocol-relative), accent underline, external rel.
 *   • KaTeX math → remark-math + rehype-katex, display math styled
 *     as a soft rounded card. `\[..\]` / `\(..\)` pre-normalized to
 *     `$$` / `$`.
 */
import React, { useCallback, useMemo, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import {
  Check,
  Copy,
  Info,
  Lightbulb,
  AlertTriangle,
  OctagonAlert,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import "katex/dist/katex.min.css";
import { cn, sanitizeLinkUrl } from "@/lib/utils";
import { parseHeadings, slugify, stripFrontmatter } from "@/lib/markdown";

/* ── LaTeX delimiter normalization ────────────────────────────────
   GPT-family models emit \[..\] / \(..\); remark-math only groks the
   $ family, so convert before render. Code fences / inline code are
   protected. Ported zero-dep from seahorse's normalize-latex.ts. */
function normalizeLatexDelimiters(text: string): string {
  const segments: string[] = [];
  let lastIndex = 0;
  const codeRegex = /```[\s\S]*?```|`[^`\n]+`/g;
  let match: RegExpExecArray | null;
  while ((match = codeRegex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      segments.push(convertDelimiters(text.slice(lastIndex, match.index)));
    }
    segments.push(match[0]);
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push(convertDelimiters(text.slice(lastIndex)));
  }
  return segments.join("");
}

function convertDelimiters(text: string): string {
  text = text.replace(/\\\[([\s\S]*?)\\\]/g, (_, inner) => "$$" + inner + "$$");
  text = text.replace(/\\\(([\s\S]*?)\\\)/g, (_, inner) => "$" + inner + "$");
  return text;
}

/* ── shared utility tokens ───────────────────────────────────────── */
const PROSE_LEADING = "leading-[1.7]";
const HEADING_BASE = "font-semibold scroll-mt-24";
const EYEBROW =
  "mt-4 mb-1 font-semibold text-[11px] uppercase tracking-[0.06em] text-subtle";

/* ── language label table for code fences ───────────────────────── */
const LANGUAGE_LABELS: Record<string, string> = {
  ts: "TypeScript",
  tsx: "TSX",
  typescript: "TypeScript",
  js: "JavaScript",
  jsx: "JSX",
  javascript: "JavaScript",
  py: "Python",
  python: "Python",
  rb: "Ruby",
  go: "Go",
  rs: "Rust",
  rust: "Rust",
  sh: "Shell",
  bash: "Shell",
  zsh: "Shell",
  shell: "Shell",
  json: "JSON",
  yaml: "YAML",
  yml: "YAML",
  toml: "TOML",
  md: "Markdown",
  mdx: "MDX",
  html: "HTML",
  xml: "XML",
  css: "CSS",
  scss: "SCSS",
  sql: "SQL",
  diff: "Diff",
  dockerfile: "Dockerfile",
  java: "Java",
  c: "C",
  cpp: "C++",
  cs: "C#",
  php: "PHP",
  swift: "Swift",
  kotlin: "Kotlin",
  kt: "Kotlin",
};

function labelFor(lang: string | undefined): string {
  if (!lang) return "Plain text";
  return LANGUAGE_LABELS[lang.toLowerCase()] ?? lang;
}

/** Detect the language hint on `<pre><code class="language-xxx">`. */
function getPreNodeLanguage(node: any): string | null {
  const firstChild = node?.children?.[0];
  if (firstChild?.tagName !== "code") return null;
  const className =
    firstChild.properties?.className?.[0] ||
    firstChild.properties?.className ||
    "";
  if (typeof className !== "string") return null;
  const match = className.match(/language-([\w+-]+)/);
  return match ? match[1] : null;
}

/** Flatten react children to raw text (clipboard copy on fences). */
function extractCodeText(children: React.ReactNode): string {
  let out = "";
  React.Children.forEach(children, (child) => {
    if (typeof child === "string") {
      out += child;
    } else if (React.isValidElement<{ children?: React.ReactNode }>(child)) {
      out += extractCodeText(child.props.children);
    }
  });
  return out;
}

/* ── CodeBlock — rounded card, chrome bar, monochrome body ───────── */
type CopyState = "idle" | "copied" | "failed";

function CodeBlock({ language, code }: { language?: string; code: string }) {
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const label = labelFor(language);

  const handleCopy = useCallback(async () => {
    const reportSuccess = () => {
      setCopyState("copied");
      setTimeout(() => setCopyState("idle"), 1600);
    };
    const reportFailure = () => {
      setCopyState("failed");
      setTimeout(() => setCopyState("idle"), 2200);
    };
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(code);
        reportSuccess();
        return;
      } catch {
        /* fall through to legacy path */
      }
    }
    // Hidden-textarea fallback for http:// (non-secure) origins where
    // navigator.clipboard is undefined.
    try {
      const ta = document.createElement("textarea");
      ta.value = code;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (ok) reportSuccess();
      else reportFailure();
    } catch {
      reportFailure();
    }
  }, [code]);

  const copied = copyState === "copied";
  const failed = copyState === "failed";

  return (
    <div className="group/code my-4 rounded-[var(--radius-lg)] border border-border bg-surface-2/50 overflow-hidden">
      <div className="flex items-center justify-between px-4 pt-2.5 pb-1.5 border-b border-border/60">
        <span className="text-[11px] font-medium tracking-wide text-foreground-muted">
          {label}
        </span>
        <button
          type="button"
          onClick={handleCopy}
          aria-label={copied ? "Copied" : failed ? "Copy failed" : "Copy code"}
          className={cn(
            "inline-flex items-center gap-1 h-6 px-2 rounded-[var(--radius-sm)] text-[11px] font-medium transition-token cursor-pointer",
            "text-foreground-muted hover:text-foreground hover:bg-surface",
            "opacity-0 group-hover/code:opacity-100 focus-visible:opacity-100",
            (copied || failed) && "opacity-100",
            "focus:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          )}
        >
          {copied ? (
            <>
              <Check className="w-3 h-3 text-[var(--color-success)]" strokeWidth={2.5} />
              <span className="text-[var(--color-success)]">Copied</span>
            </>
          ) : failed ? (
            <span className="text-[var(--color-danger)]">Failed</span>
          ) : (
            <>
              <Copy className="w-3 h-3" strokeWidth={2} />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>
      <pre className="px-4 pb-3 pt-2.5 overflow-auto max-h-[480px] text-[13px] leading-relaxed font-mono text-foreground/90 whitespace-pre">
        {code}
      </pre>
    </div>
  );
}

/* ── Callouts (Notion-style) ─────────────────────────────────────── */
type AlertKind = "note" | "tip" | "important" | "warning" | "caution";

interface AlertConfig {
  label: string;
  icon: LucideIcon;
  /** glyph color (token via arbitrary value) */
  iconClass: string;
  /** tinted card surface (color-mix off the token) */
  cardClass: string;
  /** icon-tile surface */
  tileClass: string;
}

const ALERTS: Record<AlertKind, AlertConfig> = {
  note: {
    label: "Note",
    icon: Info,
    iconClass: "text-primary",
    cardClass:
      "bg-[color-mix(in_srgb,var(--color-primary)_7%,transparent)] ring-1 ring-[color-mix(in_srgb,var(--color-primary)_20%,transparent)]",
    tileClass:
      "bg-[color-mix(in_srgb,var(--color-primary)_14%,transparent)]",
  },
  tip: {
    label: "Tip",
    icon: Lightbulb,
    iconClass: "text-[var(--color-success)]",
    cardClass:
      "bg-[color-mix(in_srgb,var(--color-success)_8%,transparent)] ring-1 ring-[color-mix(in_srgb,var(--color-success)_22%,transparent)]",
    tileClass:
      "bg-[color-mix(in_srgb,var(--color-success)_14%,transparent)]",
  },
  important: {
    label: "Important",
    icon: Sparkles,
    iconClass: "text-accent",
    cardClass:
      "bg-[color-mix(in_srgb,var(--color-accent)_8%,transparent)] ring-1 ring-[color-mix(in_srgb,var(--color-accent)_22%,transparent)]",
    tileClass:
      "bg-[color-mix(in_srgb,var(--color-accent)_14%,transparent)]",
  },
  warning: {
    label: "Warning",
    icon: AlertTriangle,
    iconClass: "text-[var(--color-warning)]",
    cardClass:
      "bg-[color-mix(in_srgb,var(--color-warning)_9%,transparent)] ring-1 ring-[color-mix(in_srgb,var(--color-warning)_24%,transparent)]",
    tileClass:
      "bg-[color-mix(in_srgb,var(--color-warning)_16%,transparent)]",
  },
  caution: {
    label: "Caution",
    icon: OctagonAlert,
    iconClass: "text-[var(--color-danger)]",
    cardClass:
      "bg-[color-mix(in_srgb,var(--color-danger)_9%,transparent)] ring-1 ring-[color-mix(in_srgb,var(--color-danger)_24%,transparent)]",
    tileClass:
      "bg-[color-mix(in_srgb,var(--color-danger)_16%,transparent)]",
  },
};

const ALERT_PATTERN = /^\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$/i;

/** Pull a leading `[!KIND]` marker out of a blockquote's children. */
function detectAlert(
  children: React.ReactNode,
): { kind: AlertKind; rest: React.ReactNode } | null {
  const arr = React.Children.toArray(children);
  if (arr.length === 0) return null;
  const first = arr[0];
  if (!React.isValidElement<{ children?: React.ReactNode }>(first)) return null;

  const innerChildren = React.Children.toArray(first.props.children);
  if (innerChildren.length === 0) return null;
  const head = innerChildren[0];
  if (typeof head !== "string") return null;

  const match = head.match(ALERT_PATTERN);
  if (!match) {
    // Marker may share the first text node with following content on
    // the next line (`[!NOTE]\nbody`).
    const lines = head.split(/\r?\n/);
    if (lines.length < 2) return null;
    const lineMatch = lines[0].match(ALERT_PATTERN);
    if (!lineMatch) return null;
    const remainder = lines.slice(1).join("\n");
    const newInner = [remainder, ...innerChildren.slice(1)];
    const newFirst = React.cloneElement(first, {}, newInner);
    return {
      kind: lineMatch[1].toLowerCase() as AlertKind,
      rest: [newFirst, ...arr.slice(1)],
    };
  }

  const newInner = innerChildren.slice(1);
  if (newInner.length === 0) {
    return { kind: match[1].toLowerCase() as AlertKind, rest: arr.slice(1) };
  }
  const newFirst = React.cloneElement(first, {}, newInner);
  return {
    kind: match[1].toLowerCase() as AlertKind,
    rest: [newFirst, ...arr.slice(1)],
  };
}

function MarkdownAlert({
  kind,
  children,
}: {
  kind: AlertKind;
  children: React.ReactNode;
}) {
  const cfg = ALERTS[kind];
  const Icon = cfg.icon;
  return (
    <div
      role="note"
      aria-label={cfg.label}
      className={cn(
        "my-4 rounded-[var(--radius-lg)] px-4 py-3.5 flex items-start gap-3",
        cfg.cardClass,
      )}
    >
      <span
        className={cn(
          "shrink-0 w-7 h-7 rounded-[var(--radius-md)] flex items-center justify-center mt-0.5",
          cfg.tileClass,
        )}
      >
        <Icon className={cn("w-4 h-4", cfg.iconClass)} strokeWidth={2.25} />
      </span>
      <div className="min-w-0 flex-1 text-[14px] text-foreground leading-relaxed [&_p:first-child]:mt-0 [&_p:last-child]:mb-0">
        {children}
      </div>
    </div>
  );
}

/* ── Task-list checkbox row (GFM `- [x]` / `- [ ]`) ──────────────── */
function TaskListItem({ children }: { children: React.ReactNode }) {
  const arr = React.Children.toArray(children);
  const checkbox = arr.find(
    (c: any) =>
      React.isValidElement(c) &&
      (c.type === "input" || (c as any).props?.type === "checkbox"),
  );
  const checked = (checkbox as any)?.props?.checked === true;
  const rest = arr.filter((c) => c !== checkbox);
  return (
    <li className="list-none -ml-6 my-1 leading-[1.7] flex items-start gap-2 [&>p]:my-0 [&>input]:hidden">
      <span
        aria-hidden
        className={cn(
          "mt-[6px] shrink-0 w-[15px] h-[15px] rounded-[5px] border transition-token",
          checked
            ? "bg-accent border-accent text-[var(--color-accent-foreground)] flex items-center justify-center"
            : "bg-surface border-border-strong",
        )}
      >
        {checked && (
          <svg
            viewBox="0 0 16 16"
            className="w-2.5 h-2.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="3"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M3 8.5 L7 12 L13 4" />
          </svg>
        )}
      </span>
      <span className={cn("min-w-0 flex-1", checked && "text-subtle line-through")}>
        {rest}
      </span>
    </li>
  );
}

/* ── Heading factory with stable, outline-matched ids ────────────── */
function flattenText(children: any): string {
  if (typeof children === "string") return children;
  if (Array.isArray(children)) return children.map(flattenText).join("");
  if (children?.props?.children) return flattenText(children.props.children);
  return "";
}

/* ── Build the full components map ───────────────────────────────── */
function buildComponents(markdown: string) {
  // Heading slugs in document order — matches parseHeadings() so the
  // outline's `#slug` anchors line up with the rendered `id`s. The
  // cursor advances once per heading element react-markdown renders,
  // mirroring the source order parseHeadings walks.
  const slugQueue = parseHeadings(markdown).map((h) => h.slug);
  let cursor = 0;
  const nextId = (children: any, level: number) =>
    slugQueue[cursor++] ?? slugify(flattenText(children)) ?? `heading-${level}`;

  const heading =
    (level: 1 | 2 | 3 | 4 | 5 | 6, cls: string) =>
    ({ node: _node, children, ...props }: any) => {
      const Tag = `h${level}` as any;
      return (
        <Tag id={nextId(children, level)} className={cls} {...props}>
          {children}
        </Tag>
      );
    };

  return {
    /* ── Block prose ──────────────────────────────────────────── */
    p: ({ node: _node, children, ...props }: any) => (
      <p className={cn(PROSE_LEADING, "my-3 wrap-break-word")} {...props}>
        {children}
      </p>
    ),

    hr: ({ node: _node, ...props }: any) => (
      <hr
        className="my-7 border-0 h-px bg-gradient-to-r from-transparent via-border-strong to-transparent"
        {...props}
      />
    ),

    /* ── Headings — tuned cascade + negative tracking ─────────── */
    h1: heading(
      1,
      cn(HEADING_BASE, "mt-8 mb-3.5 text-[1.9em] text-foreground tracking-[-0.02em] leading-tight"),
    ),
    h2: heading(
      2,
      cn(HEADING_BASE, "mt-7 mb-3 text-[1.5em] text-foreground tracking-[-0.015em] leading-snug"),
    ),
    h3: heading(
      3,
      cn(HEADING_BASE, "mt-6 mb-2.5 text-[1.25em] text-foreground tracking-[-0.01em]"),
    ),
    h4: heading(
      4,
      cn(HEADING_BASE, "mt-5 mb-2 text-[1.08em] text-foreground tracking-[-0.006em]"),
    ),
    h5: heading(5, cn(HEADING_BASE, "mt-4 mb-1.5 text-[0.95em] text-foreground-muted")),
    h6: ({ node: _node, children, level: _l, ...props }: any) => (
      <h6 id={nextId(children, 6)} className={EYEBROW} {...props}>
        {children}
      </h6>
    ),

    /* ── Inline phrase elements ───────────────────────────────── */
    strong: ({ node: _node, children, ...props }: any) => (
      <strong className="font-semibold text-foreground tracking-[-0.005em]" {...props}>
        {children}
      </strong>
    ),
    em: ({ node: _node, children, ...props }: any) => (
      <em className="italic text-foreground-muted" {...props}>
        {children}
      </em>
    ),
    del: ({ node: _node, children, ...props }: any) => (
      <del className="text-subtle" {...props}>
        {children}
      </del>
    ),
    kbd: ({ node: _node, children, ...props }: any) => (
      <kbd
        className="font-mono text-[0.825em] px-1.5 min-w-[1.5em] inline-flex items-center justify-center rounded-[var(--radius-sm)] bg-surface text-foreground border border-border align-baseline mx-[1px]"
        {...props}
      >
        {children}
      </kbd>
    ),
    sup: ({ node: _node, children, ...props }: any) => (
      <sup className="text-[0.7em] text-accent [&_a]:no-underline" {...props}>
        {children}
      </sup>
    ),
    sub: ({ node: _node, children, ...props }: any) => (
      <sub className="text-[0.7em] text-foreground-muted" {...props}>
        {children}
      </sub>
    ),

    /* ── Lists ────────────────────────────────────────────────── */
    ul: ({ node: _node, children, ...props }: any) => (
      <ul
        className={cn("pl-6 my-3 list-disc marker:text-subtle", PROSE_LEADING)}
        {...props}
      >
        {children}
      </ul>
    ),
    ol: ({ node: _node, children, ...props }: any) => (
      <ol
        className={cn(
          "pl-6 my-3 list-decimal marker:text-accent marker:font-semibold",
          PROSE_LEADING,
        )}
        {...props}
      >
        {children}
      </ol>
    ),
    li: ({ node, children, ...props }: any) => {
      const isTask =
        props?.className?.includes?.("task-list-item") ||
        node?.properties?.className?.includes?.("task-list-item");
      if (isTask) return <TaskListItem>{children}</TaskListItem>;
      return (
        <li
          className={cn("my-1 [&>p]:my-0 [&>ul]:my-1 [&>ol]:my-1", PROSE_LEADING)}
          {...props}
        >
          {children}
        </li>
      );
    },

    /* ── Links + media ────────────────────────────────────────── */
    a: ({ node: _node, href, children, ...props }: any) => {
      const safe = sanitizeLinkUrl(href);
      const external = /^https?:\/\//i.test(safe);
      return (
        <a
          href={safe}
          {...(external ? { rel: "noopener noreferrer", target: "_blank" } : {})}
          className="text-accent underline decoration-accent/40 underline-offset-[3px] decoration-1 hover:decoration-accent transition-token break-words"
          {...props}
        >
          {children}
        </a>
      );
    },
    img: ({ node: _node, src, alt, ...props }: any) => (
      <img
        src={src}
        alt={alt}
        loading="lazy"
        className="block my-4 rounded-[var(--radius-lg)] border border-border max-w-full h-auto"
        {...props}
      />
    ),

    /* ── Code (inline + fenced) ───────────────────────────────── */
    code: ({ node: _node, inline, className, children, ...props }: any) => {
      if (inline) {
        return (
          <code
            className="font-mono text-[0.875em] px-1.5 py-0.5 mx-[1px] rounded-[var(--radius-sm)] bg-surface-2 text-primary ring-1 ring-[color-mix(in_srgb,var(--color-primary)_15%,transparent)] wrap-anywhere"
            {...props}
          >
            {children}
          </code>
        );
      }
      return (
        <code className={className} {...props}>
          {children}
        </code>
      );
    },
    pre: ({ node, children }: any) => {
      const lang = getPreNodeLanguage(node);
      const code = extractCodeText(children).replace(/\n+$/, "");
      return <CodeBlock language={lang ?? undefined} code={code} />;
    },

    /* ── Blockquote → callout or quiet quote ──────────────────── */
    blockquote: ({ node: _node, children, ...props }: any) => {
      const alert = detectAlert(children);
      if (alert) return <MarkdownAlert kind={alert.kind}>{alert.rest}</MarkdownAlert>;
      return (
        <blockquote
          className={cn(
            "my-4 pl-4 pr-3 py-2 italic",
            PROSE_LEADING,
            "border-l-2 border-border-strong bg-surface-2/50 text-foreground-muted rounded-r-[var(--radius-md)]",
            "[&>p:first-child]:mt-0 [&>p:last-child]:mb-0",
          )}
          {...props}
        >
          {children}
        </blockquote>
      );
    },

    /* ── Tables ───────────────────────────────────────────────── */
    table: ({ node: _node, children, ...props }: any) => (
      <div className="my-5 overflow-x-auto max-w-full rounded-[var(--radius-lg)] border border-border">
        <table className="min-w-full border-collapse text-[0.92em]" {...props}>
          {children}
        </table>
      </div>
    ),
    thead: ({ node: _node, children, ...props }: any) => (
      <thead className="bg-surface-2" {...props}>
        {children}
      </thead>
    ),
    tbody: ({ node: _node, children, ...props }: any) => (
      <tbody
        className="[&>tr:nth-child(even)]:bg-surface-2/30 [&>tr:hover]:bg-surface-2/60"
        {...props}
      >
        {children}
      </tbody>
    ),
    tr: ({ node: _node, children, ...props }: any) => (
      <tr className="transition-token" {...props}>
        {children}
      </tr>
    ),
    th: ({ node: _node, children, ...props }: any) => (
      <th
        className="px-4 py-2.5 text-left font-semibold text-foreground-muted border-b border-border whitespace-nowrap text-[0.82em] uppercase tracking-[0.04em]"
        {...props}
      >
        {children}
      </th>
    ),
    td: ({ node: _node, children, ...props }: any) => (
      <td
        className="px-4 py-2.5 text-foreground border-b border-border/60 align-top"
        {...props}
      >
        {children}
      </td>
    ),
  };
}

const REMARK_PLUGINS = [remarkGfm, remarkMath];
const REHYPE_PLUGINS = [rehypeKatex];

export interface MarkdownRenderProps {
  markdown: string;
  className?: string;
}

/**
 * Shared markdown renderer. Wraps react-markdown with a hand-tuned
 * components map (NOT `.prose`). The outer wrapper carries the base
 * font color + a `.akb-md` scope used to style KaTeX display blocks
 * (see src/index.css).
 */
export function MarkdownRender({ markdown, className }: MarkdownRenderProps) {
  // Drop any leading embedded frontmatter block before rendering so its
  // closing `---` isn't parsed as a setext heading (see stripFrontmatter).
  // Sharing the stripped body keeps the rendered headings and the slug
  // queue built inside buildComponents in lock-step.
  const body = useMemo(() => stripFrontmatter(markdown || ""), [markdown]);
  const normalized = useMemo(
    () => normalizeLatexDelimiters(body),
    [body],
  );
  const components = useMemo(
    () => buildComponents(body),
    [body],
  );

  return (
    <div className={cn("akb-md min-w-0 text-[15px] text-foreground", className)}>
      <Markdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={components}
      >
        {normalized}
      </Markdown>
    </div>
  );
}

export default MarkdownRender;
