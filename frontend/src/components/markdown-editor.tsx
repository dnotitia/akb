import * as React from "react";
import {
  ParagraphPlugin,
  Plate,
  PlateContent,
  PlateElement,
  PlateLeaf,
  type PlateElementProps,
  type PlateLeafProps,
  useEditorRef,
  useEditorState,
  usePlateEditor,
} from "platejs/react";
import { MarkdownPlugin } from "@platejs/markdown";
import { toggleList, ListStyleType } from "@platejs/list";
import { upsertLink } from "@platejs/link";
import { insertTable } from "@platejs/table";
import {
  Bold,
  Italic,
  Strikethrough,
  Code,
  Code2,
  List,
  ListOrdered,
  Quote,
  Minus,
  Link2,
  Table as TableIcon,
  Undo2,
  Redo2,
  Heading1,
  Heading2,
  Heading3,
  Pilcrow,
} from "lucide-react";
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
import { cn, sanitizeLinkUrl } from "@/lib/utils";

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
  const url = (props.element as { url?: string }).url;
  const safe = sanitizeLinkUrl(url);
  return (
    <PlateElement
      {...props}
      as="a"
      // href is read-only in the editor; opening links is handled outside the
      // editing surface (cmd-click). We still set href for serialization round-trip.
      attributes={{ ...props.attributes, href: safe, rel: "noopener noreferrer" }}
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

// ── Formatting ribbon ─────────────────────────────────────────────────────
// A sticky toolbar rendered inside <Plate> (so the buttons can reach the live
// editor via useEditorRef / useEditorState). It only mutates the editor through
// the v53 transform/query API verified against the installed type defs:
//   marks   → editor.tf.toggleMark(key) / editor.api.marks()
//   blocks  → editor.tf.setNodes({ type }) / editor.api.block()
//   lists   → toggleList(editor, { listStyleType })  (@platejs/list)
//   link    → upsertLink(editor, { url })            (@platejs/link)
//   table   → insertTable(editor, {...})             (@platejs/table)
//   history → editor.tf.undo() / editor.tf.redo()

const TOOLBAR_BTN =
  "inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-sm)] " +
  "text-foreground-muted transition-token cursor-pointer hover:bg-surface-muted hover:text-foreground " +
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40 disabled:pointer-events-none";

const TOOLBAR_BTN_ACTIVE =
  "bg-surface text-foreground shadow-sm";

const TOOLBAR_GROUP =
  "inline-flex items-center gap-0.5 rounded-[var(--radius-md)] bg-surface-2 p-1";

interface RibbonButtonProps {
  label: string;
  active?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}

function RibbonButton({ label, active, onClick, children }: RibbonButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={active}
      title={label}
      // Prevent the editor from losing its selection when the button is pressed.
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      className={cn(TOOLBAR_BTN, active && TOOLBAR_BTN_ACTIVE)}
    >
      {children}
    </button>
  );
}

function EditorToolbar() {
  // useEditorState re-renders on editor changes so active states stay in sync;
  // useEditorRef gives a stable handle for the mutating callbacks.
  const editor = useEditorRef();
  const state = useEditorState();

  // Active mark lookup — editor.api.marks() returns the marks that would apply
  // at the current selection (null when none).
  const marks = (state.api.marks() ?? {}) as Record<string, unknown>;
  const isMark = (key: string) => Boolean(marks[key]);

  // Active block type — the highest block at the selection.
  const blockEntry = state.api.block();
  const blockType = blockEntry
    ? ((blockEntry[0] as { type?: string }).type ?? ParagraphPlugin.key)
    : undefined;
  const isBlock = (type: string) => blockType === type;

  const toggleMark = (key: string) => editor.tf.toggleMark(key);

  // Block-type toggle: set the type, or fall back to paragraph if already set.
  const setBlock = (type: string) => {
    editor.tf.setNodes({
      type: isBlock(type) ? ParagraphPlugin.key : type,
    });
  };

  const insertHr = () => {
    editor.tf.insertNodes({
      type: HorizontalRulePlugin.key,
      children: [{ text: "" }],
    });
  };

  const onLink = () => {
    // Only meaningful on a non-collapsed selection; upsertLink wraps it.
    if (state.api.isExpanded()) {
      upsertLink(editor, { url: "https://" });
    }
  };

  const onTable = () => {
    insertTable(editor, { rowCount: 3, colCount: 3, header: true });
  };

  return (
    <div
      contentEditable={false}
      className={cn(
        "sticky top-0 z-10 flex flex-wrap items-center gap-1.5",
        "border-b border-border bg-surface/90 px-2 py-1.5 backdrop-blur",
        "rounded-t-[var(--radius-sm)] select-none",
      )}
    >
      {/* Block types */}
      <div className={TOOLBAR_GROUP}>
        <RibbonButton
          label="Paragraph"
          active={isBlock(ParagraphPlugin.key)}
          onClick={() => setBlock(ParagraphPlugin.key)}
        >
          <Pilcrow className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Heading 1" active={isBlock(H1Plugin.key)} onClick={() => setBlock(H1Plugin.key)}>
          <Heading1 className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Heading 2" active={isBlock(H2Plugin.key)} onClick={() => setBlock(H2Plugin.key)}>
          <Heading2 className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Heading 3" active={isBlock(H3Plugin.key)} onClick={() => setBlock(H3Plugin.key)}>
          <Heading3 className="h-4 w-4" />
        </RibbonButton>
      </div>

      {/* Marks */}
      <div className={TOOLBAR_GROUP}>
        <RibbonButton label="Bold" active={isMark(BoldPlugin.key)} onClick={() => toggleMark(BoldPlugin.key)}>
          <Bold className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Italic" active={isMark(ItalicPlugin.key)} onClick={() => toggleMark(ItalicPlugin.key)}>
          <Italic className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton
          label="Strikethrough"
          active={isMark(StrikethroughPlugin.key)}
          onClick={() => toggleMark(StrikethroughPlugin.key)}
        >
          <Strikethrough className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Inline code" active={isMark(CodePlugin.key)} onClick={() => toggleMark(CodePlugin.key)}>
          <Code className="h-4 w-4" />
        </RibbonButton>
      </div>

      {/* Lists */}
      <div className={TOOLBAR_GROUP}>
        <RibbonButton
          label="Bulleted list"
          onClick={() => toggleList(editor, { listStyleType: ListStyleType.Disc })}
        >
          <List className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton
          label="Numbered list"
          onClick={() => toggleList(editor, { listStyleType: ListStyleType.Decimal })}
        >
          <ListOrdered className="h-4 w-4" />
        </RibbonButton>
      </div>

      {/* Blocks: quote, code block, rule */}
      <div className={TOOLBAR_GROUP}>
        <RibbonButton
          label="Blockquote"
          active={isBlock(BlockquotePlugin.key)}
          onClick={() => setBlock(BlockquotePlugin.key)}
        >
          <Quote className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton
          label="Code block"
          active={isBlock(CodeBlockPlugin.key)}
          onClick={() => setBlock(CodeBlockPlugin.key)}
        >
          <Code2 className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Horizontal rule" onClick={insertHr}>
          <Minus className="h-4 w-4" />
        </RibbonButton>
      </div>

      {/* Link + table */}
      <div className={TOOLBAR_GROUP}>
        <RibbonButton label="Insert link on selection" onClick={onLink}>
          <Link2 className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Insert table" onClick={onTable}>
          <TableIcon className="h-4 w-4" />
        </RibbonButton>
      </div>

      {/* History */}
      <div className={TOOLBAR_GROUP}>
        <RibbonButton label="Undo" onClick={() => editor.tf.undo()}>
          <Undo2 className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Redo" onClick={() => editor.tf.redo()}>
          <Redo2 className="h-4 w-4" />
        </RibbonButton>
      </div>
    </div>
  );
}

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
    value: (ed) => {
      try {
        return ed.getApi(MarkdownPlugin).markdown.deserialize(value || "");
      } catch (err) {
        // Plate's mdast deserializer can throw on malformed input
        // (unsupported HTML, broken tables, etc). Surface the editor with
        // an empty body instead of letting the whole page crash — the user
        // can still re-paste or use Raw view to recover the original.
        console.warn("MarkdownEditor: deserialize failed, mounting empty editor", err);
        return [{ type: ParagraphPlugin.key, children: [{ text: "" }] }];
      }
    },
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
      {!readOnly && <EditorToolbar />}
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
