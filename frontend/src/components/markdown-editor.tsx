import * as React from "react";
import {
  ParagraphPlugin,
  Plate,
  PlateContent,
  PlateElement,
  PlateLeaf,
  type PlateElementProps,
  type PlateLeafProps,
  usePlateEditor,
} from "platejs/react";
import { MarkdownPlugin } from "@platejs/markdown";
import {
  BlockquotePlugin,
  BoldPlugin,
  CodePlugin,
  H1Plugin,
  H2Plugin,
  H3Plugin,
  H4Plugin,
  H5Plugin,
  H6Plugin,
  HorizontalRulePlugin,
  ItalicPlugin,
  StrikethroughPlugin,
} from "@platejs/basic-nodes/react";
import {
  CodeBlockPlugin,
  CodeLinePlugin,
  CodeSyntaxPlugin,
} from "@platejs/code-block/react";
import { LinkPlugin } from "@platejs/link/react";
import { ListPlugin } from "@platejs/list/react";
import {
  TableCellHeaderPlugin,
  TableCellPlugin,
  TablePlugin,
  TableRowPlugin,
} from "@platejs/table/react";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

// ── Element & leaf components ─────────────────────────────────────────────
// AKB tone: keep prose styling close to the read view (prose dark:prose-invert
// is applied at the wrapper), so most elements just need a semantic tag with
// a small spacing/typography tweak. Headings get the same Fraunces serif
// the rendered tab uses by default through the typography plugin.

function ParagraphElement(props: PlateElementProps) {
  return <PlateElement {...props} as="p" className="my-3 leading-7" />;
}

function makeHeading(tag: "h1" | "h2" | "h3" | "h4" | "h5" | "h6", cls: string) {
  return function HeadingElement(props: PlateElementProps) {
    return <PlateElement {...props} as={tag} className={cls} />;
  };
}

function BlockquoteElement(props: PlateElementProps) {
  return (
    <PlateElement
      {...props}
      as="blockquote"
      className="border-l-2 border-border pl-4 italic text-foreground-muted my-4"
    />
  );
}

function HrElement(props: PlateElementProps) {
  // hr is a void element — children must still render for slate's selection.
  return (
    <PlateElement {...props} className="my-6">
      <div contentEditable={false}>
        <hr className="border-border" />
      </div>
      {props.children}
    </PlateElement>
  );
}

function CodeBlockElement(props: PlateElementProps) {
  return (
    <PlateElement
      {...props}
      as="pre"
      className="bg-surface-muted border border-border p-3 my-3 overflow-x-auto font-mono text-[13px] leading-[1.55]"
    />
  );
}

function CodeLineElement(props: PlateElementProps) {
  return <PlateElement {...props} as="div" />;
}

function LinkElement(props: PlateElementProps) {
  const url = (props.element as any).url as string | undefined;
  return (
    <PlateElement
      {...props}
      as="a"
      // href is read-only in the editor; opening links is handled outside the
      // editing surface (cmd-click). We still set href for serialization round-trip.
      attributes={{ ...props.attributes, href: url }}
      className="text-accent underline underline-offset-2 hover:no-underline"
    />
  );
}

function ListElement(props: PlateElementProps) {
  const type = (props.element as any).type as string | undefined;
  const Tag = type === "ol" ? "ol" : "ul";
  const cls =
    Tag === "ol"
      ? "list-decimal pl-6 my-3 space-y-1"
      : "list-disc pl-6 my-3 space-y-1";
  return <PlateElement {...props} as={Tag} className={cls} />;
}

function ListItemElement(props: PlateElementProps) {
  return <PlateElement {...props} as="li" className="leading-7" />;
}

function TableElement(props: PlateElementProps) {
  return (
    <PlateElement {...props} as="table" className="my-4 w-full border border-border text-sm" />
  );
}

function TableRowElement(props: PlateElementProps) {
  return <PlateElement {...props} as="tr" className="border-b border-border" />;
}

function TableCellElement(props: PlateElementProps) {
  return <PlateElement {...props} as="td" className="border border-border px-3 py-1.5" />;
}

function TableHeaderCellElement(props: PlateElementProps) {
  return (
    <PlateElement
      {...props}
      as="th"
      className="border border-border px-3 py-1.5 bg-surface-muted text-left font-mono text-xs uppercase"
    />
  );
}

// Marks (inline formatting) — render as semantic inline tags with utility
// classes; prose plugin will pick them up too but Plate replaces default
// rendering when a leaf component is registered.

function BoldLeaf(props: PlateLeafProps) {
  return <PlateLeaf {...props} as="strong" className="font-semibold" />;
}

function ItalicLeaf(props: PlateLeafProps) {
  return <PlateLeaf {...props} as="em" className="italic" />;
}

function CodeLeaf(props: PlateLeafProps) {
  return (
    <PlateLeaf
      {...props}
      as="code"
      className="bg-surface-muted px-1 py-0.5 font-mono text-[0.875em] border border-border"
    />
  );
}

function StrikethroughLeaf(props: PlateLeafProps) {
  return <PlateLeaf {...props} as="s" className="line-through" />;
}

// ── Plugin set + component map ────────────────────────────────────────────

const plugins = [
  // Blocks
  ParagraphPlugin,
  H1Plugin,
  H2Plugin,
  H3Plugin,
  H4Plugin,
  H5Plugin,
  H6Plugin,
  BlockquotePlugin,
  HorizontalRulePlugin,
  CodeBlockPlugin,
  CodeLinePlugin,
  CodeSyntaxPlugin,
  ListPlugin,
  LinkPlugin,
  TablePlugin,
  TableRowPlugin,
  TableCellPlugin,
  TableCellHeaderPlugin,
  // Marks
  BoldPlugin,
  ItalicPlugin,
  CodePlugin,
  StrikethroughPlugin,
  // Serializer (with GFM so tables, strikethrough, task lists round-trip).
  MarkdownPlugin.configure({
    options: { remarkPlugins: [remarkGfm] },
  }),
];

const components: Record<string, React.FC<any>> = {
  [ParagraphPlugin.key]: ParagraphElement,
  [H1Plugin.key]: makeHeading(
    "h1",
    "font-serif text-[32px] leading-[1.15] tracking-[-0.02em] mt-8 mb-4",
  ),
  [H2Plugin.key]: makeHeading(
    "h2",
    "font-serif text-[24px] leading-[1.2] tracking-[-0.015em] mt-7 mb-3",
  ),
  [H3Plugin.key]: makeHeading("h3", "font-semibold text-[19px] mt-6 mb-2"),
  [H4Plugin.key]: makeHeading("h4", "font-semibold text-[17px] mt-5 mb-2"),
  [H5Plugin.key]: makeHeading("h5", "font-semibold text-[15px] mt-4 mb-2"),
  [H6Plugin.key]: makeHeading(
    "h6",
    "font-mono uppercase tracking-wider text-[12px] text-foreground-muted mt-4 mb-2",
  ),
  [BlockquotePlugin.key]: BlockquoteElement,
  [HorizontalRulePlugin.key]: HrElement,
  [CodeBlockPlugin.key]: CodeBlockElement,
  [CodeLinePlugin.key]: CodeLineElement,
  [LinkPlugin.key]: LinkElement,
  [ListPlugin.key]: ListElement,
  // Plate v53's ListPlugin handles ul/ol/li internally; the `type` of the
  // element drives the tag we render in ListElement above.
  [TablePlugin.key]: TableElement,
  [TableRowPlugin.key]: TableRowElement,
  [TableCellPlugin.key]: TableCellElement,
  [TableCellHeaderPlugin.key]: TableHeaderCellElement,
  // Marks
  [BoldPlugin.key]: BoldLeaf,
  [ItalicPlugin.key]: ItalicLeaf,
  [CodePlugin.key]: CodeLeaf,
  [StrikethroughPlugin.key]: StrikethroughLeaf,
};

// ── Public component ──────────────────────────────────────────────────────

export interface MarkdownEditorProps {
  /** Initial markdown body. Component is uncontrolled after mount — change
   * the `key` prop on the parent to remount with a new initial value. */
  value: string;
  /** Called with the serialized markdown on every edit. */
  onChange?: (markdown: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  readOnly?: boolean;
  className?: string;
}

export function MarkdownEditor({
  value,
  onChange,
  placeholder = "Write in markdown — slash commands and shortcuts work.",
  autoFocus,
  readOnly,
  className,
}: MarkdownEditorProps) {
  const editor = usePlateEditor({
    plugins,
    components,
    value: (ed) => ed.getApi(MarkdownPlugin).markdown.deserialize(value || ""),
  });

  return (
    <Plate
      editor={editor}
      onChange={({ editor: ed }) => {
        if (!onChange) return;
        // Serialize on every change — for documents in the typical AKB size
        // (single-digit KB markdown), this is well under a millisecond. Move
        // to a debounce only if profiling shows the cost.
        const md = ed.getApi(MarkdownPlugin).markdown.serialize();
        onChange(md);
      }}
    >
      <PlateContent
        autoFocus={autoFocus}
        readOnly={readOnly}
        placeholder={placeholder}
        className={cn(
          "min-h-[360px] w-full outline-none cursor-text",
          // `prose` defaults to max-width: 65ch — explicitly override so
          // the editor expands to its container in Edit mode (typography
          // plugin's selector beats a plain `max-w-none`).
          "prose dark:prose-invert !max-w-none",
          "font-sans text-[15px] leading-7 text-foreground",
          // PlateContent renders a div whose direct children are blocks; we
          // want the editor to look like an article surface, not a textarea.
          "border border-border bg-surface px-5 py-4",
          "hover:border-foreground-muted focus-within:border-accent focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2 focus-within:ring-offset-background transition-colors",
          // Plate marks the first empty leaf with `data-slate-placeholder`
          // when the editor is empty; surface it so a blank editor isn't a
          // mysterious silent box.
          "[&_[data-slate-placeholder=true]]:text-foreground-muted [&_[data-slate-placeholder=true]]:italic",
          className,
        )}
      />
    </Plate>
  );
}

export default MarkdownEditor;
