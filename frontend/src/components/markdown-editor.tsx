import * as React from "react";
import {
  ParagraphPlugin,
  Plate,
  PlateContent,
  PlateElement,
  PlateLeaf,
  type PlateElementProps,
  type PlateLeafProps,
  createPlatePlugin,
  useEditorRef,
  useEditorState,
  usePlateEditor,
} from "platejs/react";
import { MarkdownPlugin } from "@platejs/markdown";
import { toggleList, ListStyleType } from "@platejs/list";
import { unwrapLink, upsertLink } from "@platejs/link";
import { toggleCodeBlock } from "@platejs/code-block";
import {
  deleteColumn,
  deleteRow,
  insertTable,
  insertTableColumn,
  insertTableRow,
} from "@platejs/table";
import { TrailingBlockPlugin } from "@platejs/utils";
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
  ImagePlus,
  Loader2,
  Pilcrow,
  RotateCcw,
  Rows3,
  Columns3,
  CornerDownLeft,
  Trash2,
  X,
  Pencil,
  Replace,
  Rows2,
  Columns2,
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
import { AssetImage } from "@/components/asset-image";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { discardAsset, uploadAsset } from "@/lib/api";
import { normalizeEditorLinkUrl } from "@/lib/editor-link";
import {
  assetIdFromUrl,
  classifyEditorImageUploadFailure,
  EDITOR_IMAGE_MIME_TYPES,
  prepareEditorImage,
  validateEditorImage,
} from "@/lib/image-assets";
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
      className="text-link underline underline-offset-2 hover:no-underline"
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

function TableElement(props: PlateElementProps) {
  const editor = useEditorRef();
  const editorLifecycle = React.useContext(EditorAssetLifecycleContext);

  const withTablePath = (action: (path: number[]) => void) => {
    const path = editor.api.findPath(props.element);
    if (!path) return;
    action(path);
  };

  const focusEditor = () => {
    // Keep pointer and keyboard users in the authoring flow after a block
    // action instead of leaving focus stranded on the caption toolbar.
    requestAnimationFrame(() => editorLifecycle?.focusEditor());
  };

  const ensureCellSelection = (tablePath: number[]) => {
    const cellEntry = editor.api.above({
      match: (node) => {
        const type = (node as { type?: string } | null)?.type;
        return type === TableCellPlugin.key || type === TableCellHeaderPlugin.key;
      },
    });
    const belongsToThisTable =
      cellEntry &&
      tablePath.every((segment, index) => cellEntry[1][index] === segment);
    if (!belongsToThisTable) {
      editor.tf.select(editor.api.start([...tablePath, 0, 0]));
    }
  };

  const continueBelow = () => {
    withTablePath((path) => {
      const nextPath = [...path];
      nextPath[nextPath.length - 1] += 1;
      const nextEntry = editor.api.node(nextPath);
      const nextType = (nextEntry?.[0] as { type?: string } | undefined)?.type;
      if (nextType !== ParagraphPlugin.key) {
        editor.tf.insertNodes(
          { type: ParagraphPlugin.key, children: [{ text: "" }] },
          { at: nextPath },
        );
      }
      editor.tf.select(editor.api.start(nextPath));
      focusEditor();
    });
  };

  const deleteCurrentTable = () => {
    withTablePath((path) => {
      editor.tf.removeNodes({ at: path });
      // TrailingBlockPlugin covers a terminal table. If another block follows,
      // it shifts into the removed table's path and becomes the natural target.
      if (!editor.api.node(path)) {
        editor.tf.insertNodes(
          { type: ParagraphPlugin.key, children: [{ text: "" }] },
          { at: path },
        );
      }
      editor.tf.select(editor.api.start(path));
      focusEditor();
    });
  };

  // Wide tables scroll within themselves instead of pushing the whole
  // authoring surface into a page-level horizontal scroll.
  return (
    <PlateElement
      {...props}
      as="div"
      className="my-4 max-w-full overflow-x-auto"
    >
      <table
        aria-label={editorLifecycle?.readOnly ? "Table" : "Editable table"}
        className="!table !overflow-visible w-full min-w-[36rem] border-collapse border border-border text-sm"
      >
        {!editorLifecycle?.readOnly && (
          <caption
            contentEditable={false}
            className="caption-top border-b border-border bg-surface-2 px-2 py-1.5 text-left"
          >
            <div className="flex min-w-max items-center justify-between gap-3">
              <span className="inline-flex items-center gap-1.5 px-1 text-xs font-medium text-foreground-muted">
                <TableIcon className="h-3.5 w-3.5" aria-hidden />
                Table
              </span>
              <div className="flex items-center gap-1" role="toolbar" aria-label="Table actions">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 px-2"
                  title="Add row to the end"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    withTablePath((path) => {
                      insertTableRow(editor, { at: path, select: true });
                      focusEditor();
                    });
                  }}
                >
                  <Rows3 className="h-3.5 w-3.5" aria-hidden />
                  Row
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 px-2"
                  title="Add column to the end"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    withTablePath((path) => {
                      insertTableColumn(editor, { at: path, select: true });
                      focusEditor();
                    });
                  }}
                >
                  <Columns3 className="h-3.5 w-3.5" aria-hidden />
                  Column
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 px-2"
                  title="Remove the selected row"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    withTablePath((path) => {
                      ensureCellSelection(path);
                      deleteRow(editor);
                      focusEditor();
                    });
                  }}
                >
                  <Rows2 className="h-3.5 w-3.5" aria-hidden />
                  Remove row
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 px-2"
                  title="Remove the selected column"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    withTablePath((path) => {
                      ensureCellSelection(path);
                      deleteColumn(editor);
                      focusEditor();
                    });
                  }}
                >
                  <Columns2 className="h-3.5 w-3.5" aria-hidden />
                  Remove column
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 px-2"
                  title="Move the cursor to a paragraph below this table"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={continueBelow}
                >
                  <CornerDownLeft className="h-3.5 w-3.5" aria-hidden />
                  Continue below
                </Button>
                <span className="mx-0.5 h-4 w-px bg-border" aria-hidden />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label="Delete table"
                  title="Delete table (you can undo this action)"
                  className="h-8 w-8 text-foreground-muted hover:bg-destructive/10 hover:text-destructive"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={deleteCurrentTable}
                >
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                </Button>
              </div>
            </div>
          </caption>
        )}
        <tbody>{props.children}</tbody>
      </table>
    </PlateElement>
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

interface EditorAssetLifecycle {
  vault: string;
  document?: string;
  commit?: string;
  readOnly?: boolean;
  focusEditor: () => void;
  requestImageReplacement: (path: number[]) => void;
}

const EditorAssetLifecycleContext = React.createContext<EditorAssetLifecycle | null>(null);

function ImageElement(props: PlateElementProps) {
  const editor = useEditorRef();
  const assetLifecycle = React.useContext(EditorAssetLifecycleContext);
  const element = props.element as {
    url?: string;
    caption?: Array<{ text?: string }>;
  };
  const alt = element.caption?.map((part) => part.text || "").join("") || "";
  const [descriptionOpen, setDescriptionOpen] = React.useState(false);
  const [descriptionDraft, setDescriptionDraft] = React.useState(alt);
  const [descriptionError, setDescriptionError] = React.useState("");

  React.useEffect(() => setDescriptionDraft(alt), [alt]);

  const removeImage = () => {
    const path = editor.api.findPath(props.element);
    if (!path) return;
    editor.tf.removeNodes({ at: path });
    if (!editor.api.node(path)) {
      editor.tf.insertNodes(
        { type: ParagraphPlugin.key, children: [{ text: "" }] },
        { at: path },
      );
    }
    editor.tf.select(editor.api.start(path));
    requestAnimationFrame(() => assetLifecycle?.focusEditor());
  };

  const saveDescription = () => {
    const next = descriptionDraft.trim();
    if (!next) {
      setDescriptionError("Describe the image so it remains understandable without sight.");
      return;
    }
    const path = editor.api.findPath(props.element);
    if (!path) return;
    editor.tf.setNodes({ caption: [{ text: next }] }, { at: path });
    setDescriptionError("");
    setDescriptionOpen(false);
    requestAnimationFrame(() => assetLifecycle?.focusEditor());
  };

  const replaceImage = () => {
    const path = editor.api.findPath(props.element);
    if (!path) return;
    assetLifecycle?.requestImageReplacement(path);
  };

  return (
    <PlateElement {...props} className="my-4">
      <figure contentEditable={false} className="group m-0">
        <div className="relative mx-auto w-fit max-w-full">
          <AssetImage
            src={element.url}
            alt={alt}
            assetContext={
              assetLifecycle
                ? {
                    mode: "authenticated",
                    vault: assetLifecycle.vault,
                    document: assetLifecycle.document,
                    commit: assetLifecycle.commit,
                  }
                : undefined
            }
            className="my-0 max-h-[70vh] max-w-full object-contain"
          />
          {!assetLifecycle?.readOnly && (
            <div className="absolute right-2 top-2 flex items-center gap-1 rounded-[var(--radius-md)] border border-border bg-surface/90 p-1 shadow-sm backdrop-blur-sm">
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={alt ? `Edit image description: ${alt}` : "Edit image description"}
                title="Edit image description"
                className="h-7 w-7 text-foreground-muted hover:bg-surface-hover hover:text-foreground"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  setDescriptionDraft(alt);
                  setDescriptionError("");
                  setDescriptionOpen(true);
                }}
              >
                <Pencil className="h-3.5 w-3.5" aria-hidden />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={alt ? `Replace image: ${alt}` : "Replace image"}
                title="Replace image"
                className="h-7 w-7 text-foreground-muted hover:bg-surface-hover hover:text-foreground"
                onMouseDown={(event) => event.preventDefault()}
                onClick={replaceImage}
              >
                <Replace className="h-3.5 w-3.5" aria-hidden />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={alt ? `Remove image: ${alt}` : "Remove image"}
                title="Remove image"
                className="h-7 w-7 text-foreground-muted hover:bg-destructive/10 hover:text-destructive"
                onMouseDown={(event) => event.preventDefault()}
                onClick={removeImage}
              >
                <X className="h-3.5 w-3.5" aria-hidden />
              </Button>
            </div>
          )}
        </div>
        {alt && (
          <figcaption className="mt-1.5 text-center text-xs text-foreground-muted">
            {alt}
          </figcaption>
        )}
      </figure>
      {props.children}
      <Dialog open={descriptionOpen} onOpenChange={setDescriptionOpen}>
        <DialogContent
          className="sm:max-w-md"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            assetLifecycle?.focusEditor();
          }}
        >
          <DialogHeader>
            <DialogTitle>Image description</DialogTitle>
            <DialogDescription>
              This text is used as the image alt text and visible caption.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="editor-image-description">Description</Label>
            <Input
              id="editor-image-description"
              value={descriptionDraft}
              onChange={(event) => {
                setDescriptionDraft(event.target.value);
                if (descriptionError) setDescriptionError("");
              }}
              aria-invalid={descriptionError ? true : undefined}
              aria-describedby={descriptionError ? "editor-image-description-error" : undefined}
              autoFocus
            />
            {descriptionError && (
              <p id="editor-image-description-error" role="alert" className="text-xs text-destructive">
                {descriptionError}
              </p>
            )}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setDescriptionOpen(false)}>
              Cancel
            </Button>
            <Button type="button" onClick={saveDescription}>Save description</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PlateElement>
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

const ImagePlugin = createPlatePlugin({
  key: "img",
  node: { isElement: true, isVoid: true },
});

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
  // Markdown/GFM has no merged-cell representation. Keeping table editing in
  // rectangular mode makes row/column transforms deterministic and prevents
  // an unround-trippable editor state.
  TablePlugin.configure({ options: { disableMerge: true } }),
  TableRowPlugin,
  TableCellPlugin,
  TableCellHeaderPlugin,
  ImagePlugin,
  // Full editors must always have a text block after terminal atomic blocks
  // (tables, images, rules). This is the keyboard and pointer escape route.
  TrailingBlockPlugin,
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
    "font-semibold text-[32px] leading-[1.15] tracking-[-0.02em] mt-8 mb-4",
  ),
  [H2Plugin.key]: makeHeading(
    "h2",
    "font-semibold text-[24px] leading-[1.2] tracking-[-0.015em] mt-7 mb-3",
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
  [ImagePlugin.key]: ImageElement,
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
  "text-foreground-muted transition-token cursor-pointer hover:bg-surface-hover hover:text-foreground " +
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 disabled:pointer-events-none";

// An engaged format button is a SELECTED state — teal-tinted, hue-coded.
const TOOLBAR_BTN_ACTIVE =
  "bg-surface-selected text-surface-selected-foreground";

const TOOLBAR_GROUP =
  "inline-flex items-center gap-0.5 rounded-[var(--radius-md)] bg-surface-2 p-1";

function transferredImages(transfer: DataTransfer): File[] {
  return Array.from(transfer.files).filter((file) => file.type.startsWith("image/"));
}

function isStandaloneImageClipboard(transfer: DataTransfer): boolean {
  const html = transfer.getData("text/html").trim();
  if (!html) {
    // File-manager copy commonly supplies the image File plus only a plain
    // filename. With no rich HTML payload to preserve, the binary file is the
    // authoritative clipboard flavor and should enter the upload path. If the
    // plain flavor contains unrelated prose, keep that prose on the normal
    // editor path instead of guessing that it is disposable metadata.
    const plain = transfer.getData("text/plain").trim();
    if (!plain) return true;
    const imageNames = new Set(transferredImages(transfer).map((file) => file.name));
    const lines = plain.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    return lines.length > 0 && lines.every((line) => imageNames.has(line));
  }

  // Browser "Copy image" commonly provides both an image File and an HTML
  // <img> flavor, sometimes with a plain-text label or URL. Rich document
  // clipboard payloads from office tools also carry image Files, but their
  // HTML contains meaningful text. Remove image/metadata-only nodes and let
  // the normal Plate paste path keep the payload whenever text remains.
  const template = document.createElement("template");
  template.innerHTML = html;
  if (!template.content.querySelector("img")) return false;
  for (const node of template.content.querySelectorAll("img, meta, link, style")) {
    node.remove();
  }
  return !(template.content.textContent ?? "").trim();
}

interface RibbonButtonProps {
  label: string;
  active?: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}

function RibbonButton({ label, active, disabled, onClick, children }: RibbonButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={active}
      disabled={disabled}
      tabIndex={-1}
      data-editor-toolbar-button
      title={label}
      // Prevent the editor from losing its selection when the button is pressed.
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      className={cn(TOOLBAR_BTN, active && TOOLBAR_BTN_ACTIVE)}
    >
      <span aria-hidden="true" className="contents">{children}</span>
    </button>
  );
}

interface EditorToolbarProps {
  uploadingImage: boolean;
  onChooseImages: (files: File[]) => void;
  onOpenImagePicker: () => void;
  appearance: "framed" | "canvas" | "workspace";
  imageInputRef: React.RefObject<HTMLInputElement | null>;
}

function EditorToolbar({
  uploadingImage,
  onChooseImages,
  onOpenImagePicker,
  appearance,
  imageInputRef,
}: EditorToolbarProps) {
  // useEditorState re-renders on editor changes so active states stay in sync;
  // useEditorRef gives a stable handle for the mutating callbacks.
  const editor = useEditorRef();
  const state = useEditorState();
  const toolbarRef = React.useRef<HTMLDivElement>(null);
  const [linkOpen, setLinkOpen] = React.useState(false);
  const [linkUrl, setLinkUrl] = React.useState("");
  const [linkText, setLinkText] = React.useState("");
  const [linkError, setLinkError] = React.useState("");
  const linkSelectionRef = React.useRef(editor.selection);
  const linkUrlInputRef = React.useRef<HTMLInputElement>(null);
  const linkUrlId = React.useId();
  const linkTextId = React.useId();
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
  const blockNode = blockEntry?.[0] as {
    listStyleType?: string;
  } | undefined;
  const codeBlockEntry = state.api.above({
    match: (node) =>
      typeof node === "object" &&
      node !== null &&
      "type" in node &&
      (node as { type?: string }).type === CodeBlockPlugin.key,
  });
  const linkEntry = state.api.above({
    match: (node) =>
      typeof node === "object" &&
      node !== null &&
      "type" in node &&
      (node as { type?: string }).type === LinkPlugin.key,
  });
  const canUndo = state.history.undos.length > 0;
  const canRedo = state.history.redos.length > 0;

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

  const openLinkEditor = () => {
    linkSelectionRef.current = editor.selection;
    const currentLink = linkEntry?.[0] as { url?: string } | undefined;
    const selectedText = editor.selection
      ? editor.api.string(editor.selection)
      : "";
    setLinkUrl(currentLink?.url ?? "");
    setLinkText(
      linkEntry
        ? editor.api.string(linkEntry[1])
        : selectedText,
    );
    setLinkError("");
    setLinkOpen(true);
  };

  const applyLink = () => {
    const normalizedUrl = normalizeEditorLinkUrl(linkUrl);
    if (!normalizedUrl) {
      setLinkError("Enter an http(s), email, phone, anchor, or relative URL.");
      requestAnimationFrame(() => linkUrlInputRef.current?.focus());
      return;
    }
    if (linkSelectionRef.current) editor.tf.select(linkSelectionRef.current);
    const text = linkText.trim() || normalizedUrl;
    const inserted = upsertLink(editor, {
      url: normalizedUrl,
      text,
      skipValidation: true,
    });
    if (!inserted) {
      setLinkError("The link could not be applied at this cursor position.");
      return;
    }
    setLinkOpen(false);
  };

  const removeCurrentLink = () => {
    if (linkSelectionRef.current) editor.tf.select(linkSelectionRef.current);
    unwrapLink(editor);
    setLinkOpen(false);
  };

  const onTable = () => {
    insertTable(editor, { rowCount: 3, colCount: 3, header: true });
  };

  const toolbarGroupClass = appearance !== "framed"
    ? "inline-flex items-center gap-0.5 border-r border-border pr-1.5 last:border-r-0 last:pr-0"
    : TOOLBAR_GROUP;

  React.useLayoutEffect(() => {
    const buttons = toolbarRef.current?.querySelectorAll<HTMLButtonElement>(
      "button[data-editor-toolbar-button]:not(:disabled)",
    );
    if (!buttons?.length) return;
    if (!Array.from(buttons).some((button) => button.tabIndex === 0)) {
      buttons[0].tabIndex = 0;
    }
  });

  const handleToolbarKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!(event.target instanceof HTMLButtonElement)) return;
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const buttons = Array.from(
      toolbarRef.current?.querySelectorAll<HTMLButtonElement>(
        "button[data-editor-toolbar-button]:not(:disabled)",
      ) ?? [],
    );
    if (buttons.length === 0) return;
    event.preventDefault();
    const currentIndex = Math.max(0, buttons.indexOf(event.target));
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? buttons.length - 1
        : (currentIndex + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
    for (const button of buttons) button.tabIndex = -1;
    buttons[nextIndex].tabIndex = 0;
    buttons[nextIndex].focus();
  };

  return (
    <div
      ref={toolbarRef}
      contentEditable={false}
      role="toolbar"
      aria-label="Text formatting"
      aria-orientation="horizontal"
      onFocusCapture={(event) => {
        if (!(event.target instanceof HTMLButtonElement)) return;
        for (const button of toolbarRef.current?.querySelectorAll<HTMLButtonElement>(
          "button[data-editor-toolbar-button]",
        ) ?? []) {
          button.tabIndex = button === event.target ? 0 : -1;
        }
      }}
      onKeyDown={handleToolbarKeyDown}
      className={cn(
        "sticky top-0 z-10 flex flex-wrap items-center gap-1.5",
        "border-b border-border select-none",
        appearance === "canvas"
          ? "bg-surface/95 px-5 py-2 backdrop-blur-sm sm:px-8 lg:px-10"
          : appearance === "workspace"
            ? "bg-surface px-3 py-2"
            : "rounded-t-[var(--radius-sm)] bg-surface px-2 py-1.5",
      )}
    >
      {/* Block types */}
      <div className={toolbarGroupClass} role="group" aria-label="Block type">
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
      <div className={toolbarGroupClass} role="group" aria-label="Marks">
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
      <div className={toolbarGroupClass} role="group" aria-label="Lists">
        <RibbonButton
          label="Bulleted list"
          active={blockNode?.listStyleType === ListStyleType.Disc}
          onClick={() => toggleList(editor, { listStyleType: ListStyleType.Disc })}
        >
          <List className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton
          label="Numbered list"
          active={blockNode?.listStyleType === ListStyleType.Decimal}
          onClick={() => toggleList(editor, { listStyleType: ListStyleType.Decimal })}
        >
          <ListOrdered className="h-4 w-4" />
        </RibbonButton>
      </div>

      {/* Blocks: quote, code block, rule */}
      <div className={toolbarGroupClass} role="group" aria-label="Blocks">
        <RibbonButton
          label="Blockquote"
          active={isBlock(BlockquotePlugin.key)}
          onClick={() => setBlock(BlockquotePlugin.key)}
        >
          <Quote className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton
          label="Code block"
          active={Boolean(codeBlockEntry)}
          onClick={() => toggleCodeBlock(editor)}
        >
          <Code2 className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Horizontal rule" onClick={insertHr}>
          <Minus className="h-4 w-4" />
        </RibbonButton>
      </div>

      {/* Link + table */}
      <div className={toolbarGroupClass} role="group" aria-label="Insert">
        <RibbonButton
          label={linkEntry ? "Edit link" : "Insert link"}
          active={Boolean(linkEntry)}
          onClick={openLinkEditor}
        >
          <Link2 className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Insert table" onClick={onTable}>
          <TableIcon className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton
          label={uploadingImage ? "Uploading image" : "Insert image"}
          disabled={uploadingImage}
          onClick={onOpenImagePicker}
        >
          {uploadingImage ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <ImagePlus className="h-4 w-4" />
          )}
        </RibbonButton>
        <input
          ref={imageInputRef}
          type="file"
          accept={EDITOR_IMAGE_MIME_TYPES.join(",")}
          className="sr-only"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(event) => {
            const files = Array.from(event.currentTarget.files || []);
            event.currentTarget.value = "";
            if (files.length > 0) onChooseImages(files);
          }}
        />
      </div>

      {/* History */}
      <div className={toolbarGroupClass} role="group" aria-label="History">
        <RibbonButton label="Undo" disabled={!canUndo} onClick={() => editor.tf.undo()}>
          <Undo2 className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Redo" disabled={!canRedo} onClick={() => editor.tf.redo()}>
          <Redo2 className="h-4 w-4" />
        </RibbonButton>
      </div>
      <Dialog open={linkOpen} onOpenChange={setLinkOpen}>
        <DialogContent
          className="sm:max-w-md"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            editor.tf.focus();
          }}
        >
          <DialogHeader>
            <DialogTitle>{linkEntry ? "Edit link" : "Insert link"}</DialogTitle>
            <DialogDescription>
              Add a safe destination and choose the text readers will see.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor={linkUrlId}>URL</Label>
              <Input
                ref={linkUrlInputRef}
                id={linkUrlId}
                value={linkUrl}
                onChange={(event) => {
                  setLinkUrl(event.target.value);
                  if (linkError) setLinkError("");
                }}
                placeholder="https://example.com"
                inputMode="url"
                autoComplete="url"
                aria-invalid={linkError ? true : undefined}
                aria-describedby={linkError ? `${linkUrlId}-error` : undefined}
                autoFocus
              />
              {linkError && (
                <p id={`${linkUrlId}-error`} role="alert" className="text-xs text-destructive">
                  {linkError}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor={linkTextId}>Text</Label>
              <Input
                id={linkTextId}
                value={linkText}
                onChange={(event) => setLinkText(event.target.value)}
                placeholder="Link text"
              />
              <p className="text-xs text-foreground-muted">
                Leave blank to use the destination as the visible text.
              </p>
            </div>
          </div>
          <DialogFooter className="sm:justify-between">
            {linkEntry ? (
              <Button type="button" variant="ghost" className="text-destructive" onClick={removeCurrentLink}>
                Remove link
              </Button>
            ) : (
              <span aria-hidden />
            )}
            <div className="flex flex-col-reverse gap-2 sm:flex-row">
              <Button type="button" variant="outline" onClick={() => setLinkOpen(false)}>
                Cancel
              </Button>
              <Button type="button" onClick={applyLink}>
                {linkEntry ? "Save link" : "Insert link"}
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function nodeText(node: unknown): string {
  if (!node || typeof node !== "object") return "";
  if ("text" in node && typeof (node as { text?: unknown }).text === "string") {
    return (node as { text: string }).text;
  }
  const children = (node as { children?: unknown[] }).children;
  return Array.isArray(children) ? children.map(nodeText).join("") : "";
}

function valueWithoutEditorTrailingBlock(nodes: readonly unknown[]): unknown[] {
  if (nodes.length < 2) return [...nodes];
  const last = nodes.at(-1) as { type?: string } | undefined;
  const previous = nodes.at(-2) as { type?: string } | undefined;
  const isEmptyTrailingParagraph =
    last?.type === ParagraphPlugin.key &&
    nodeText(last).replace(/[\u200b\ufeff]/g, "").length === 0 &&
    previous?.type !== ParagraphPlugin.key;
  return isEmptyTrailingParagraph ? nodes.slice(0, -1) : [...nodes];
}

// ── Public component ──────────────────────────────────────────────────────

export interface MarkdownEditorProps {
  /** Initial markdown body. Component is uncontrolled after mount — change
   * the `key` prop on the parent to remount with a new initial value. */
  value: string;
  /** Called with the serialized Markdown and its canonical image-node ids. */
  onChange?: (markdown: string, assetIds: readonly string[]) => void;
  placeholder?: string;
  autoFocus?: boolean;
  readOnly?: boolean;
  className?: string;
  /** Visual chrome around the editor. Canvas mode removes textarea-like
   * framing so the surrounding composer can provide one continuous surface;
   * workspace mode keeps the same borderless editor inside a parent frame. */
  appearance?: "framed" | "canvas" | "workspace";
  /** Accessible name for the contenteditable region (it carries role=textbox
   * but no native label). Pass one of these so SR users hear the field name. */
  ariaLabel?: string;
  ariaLabelledby?: string;
  required?: boolean;
  /** Vault receiving pasted, dropped, or picked image assets. */
  vault: string;
  /** Exact source identity used when rendering a retained Git revision. */
  document?: string;
  commit?: string;
  /** Lets the owning form block save/navigation while an upload is in flight. */
  onUploadingChange?: (uploading: boolean) => void;
  /** Keep uploaded assets for an in-flight document save to claim. The server
   * GC remains the fallback when the save ultimately fails after navigation. */
  preserveUploadsOnUnmount?: boolean;
  /** Image-node ids from the exact editor snapshot accepted by the server. */
  claimedAssetIds?: readonly string[] | null;
  /** Temporary image ids recovered with a locally saved new-document draft. */
  initialUnclaimedAssetIds?: readonly string[];
}

export function MarkdownEditor({
  value,
  onChange,
  placeholder = "Write in markdown — slash commands and shortcuts work.",
  autoFocus,
  readOnly,
  className,
  appearance = "framed",
  ariaLabel,
  ariaLabelledby,
  required,
  vault,
  document,
  commit,
  onUploadingChange,
  preserveUploadsOnUnmount = false,
  claimedAssetIds = null,
  initialUnclaimedAssetIds = [],
}: MarkdownEditorProps) {
  const [uploadingImage, setUploadingImage] = React.useState(false);
  const [uploadingName, setUploadingName] = React.useState("");
  const [uploadFailure, setUploadFailure] = React.useState<{
    files: File[];
    message: string;
    retryable: boolean;
    kind: "error" | "queued";
    replacementPath?: number[];
  } | null>(null);
  const uploadControllerRef = React.useRef<AbortController | null>(null);
  const imageInputRef = React.useRef<HTMLInputElement>(null);
  const replacementPathRef = React.useRef<number[] | null>(null);
  const editorSurfaceRef = React.useRef<HTMLDivElement | null>(null);
  const uploadInFlightRef = React.useRef(false);
  const deferredImageFilesRef = React.useRef<File[]>([]);
  const unclaimedAssetIdsRef = React.useRef(new Set(initialUnclaimedAssetIds));
  const discardingAssetIdsRef = React.useRef(new Set<string>());
  const mountedRef = React.useRef(true);
  const onUploadingChangeRef = React.useRef(onUploadingChange);
  const preserveUploadsOnUnmountRef = React.useRef(preserveUploadsOnUnmount);

  const discardUnclaimedAsset = React.useCallback(
    (assetId: string) => {
      if (
        !unclaimedAssetIdsRef.current.has(assetId) ||
        discardingAssetIdsRef.current.has(assetId)
      ) return;
      discardingAssetIdsRef.current.add(assetId);
      void discardAsset(vault, assetId)
        .then(() => unclaimedAssetIdsRef.current.delete(assetId))
        .catch(() => {
          // Retain the id for a later unmount retry. Server-side TTL cleanup is
          // the final backstop for an abrupt browser termination.
        })
        .finally(() => discardingAssetIdsRef.current.delete(assetId));
    },
    [vault],
  );

  const discardIfUnclaimed = React.useCallback(
    (url: string | undefined) => {
      const assetId = assetIdFromUrl(url);
      if (!assetId || !unclaimedAssetIdsRef.current.has(assetId)) return;
      discardUnclaimedAsset(assetId);
    },
    [discardUnclaimedAsset],
  );

  React.useEffect(() => {
    onUploadingChangeRef.current = onUploadingChange;
  }, [onUploadingChange]);

  React.useEffect(() => {
    preserveUploadsOnUnmountRef.current = preserveUploadsOnUnmount;
  }, [preserveUploadsOnUnmount]);

  React.useEffect(() => {
    // React StrictMode replays effects in development; reset the guard on the
    // second setup so completed uploads can still update their local status.
    const unclaimedAssetIds = unclaimedAssetIdsRef.current;
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      uploadControllerRef.current?.abort();
      if (!preserveUploadsOnUnmountRef.current) {
        for (const assetId of unclaimedAssetIds) {
          discardUnclaimedAsset(assetId);
        }
      }
      onUploadingChangeRef.current?.(false);
    };
  }, [discardUnclaimedAsset]);

  const editor = usePlateEditor({
    plugins,
    components,
    value: (ed) => {
      try {
        const nodes = ed.getApi(MarkdownPlugin).markdown.deserialize(value || "");
        const lastNode = nodes.at(-1) as { type?: string } | undefined;
        // Initial markdown is deserialized before the normalizer's first edit.
        // Seed the same invariant that TrailingBlockPlugin maintains later so
        // a document loaded with a terminal table/image is immediately escapable.
        if (lastNode?.type !== ParagraphPlugin.key) {
          return [
            ...nodes,
            { type: ParagraphPlugin.key, children: [{ text: "" }] },
          ];
        }
        return nodes;
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

  React.useLayoutEffect(() => {
    if (claimedAssetIds === null) return;
    const accepted = new Set(claimedAssetIds);
    for (const assetId of unclaimedAssetIdsRef.current) {
      if (accepted.has(assetId)) {
        unclaimedAssetIdsRef.current.delete(assetId);
      } else {
        discardUnclaimedAsset(assetId);
      }
    }
  }, [claimedAssetIds, discardUnclaimedAsset]);

  const uploadImages = React.useCallback(
    async (
      files: File[],
      insertionRange = editor.selection,
      replacementPath?: number[],
    ) => {
      if (readOnly || uploadInFlightRef.current || files.length === 0) return;

      for (const file of files) {
        const validationMessage = validateEditorImage(file);
        if (validationMessage) {
          setUploadFailure({
            // Validation is a preflight over the entire batch, so no earlier
            // file has uploaded yet. Reject the batch explicitly instead of
            // presenting a partial retry that silently omits valid files.
            files: [],
            message: `${file.name}: ${validationMessage} No images were uploaded.`,
            retryable: false,
            kind: "error",
            replacementPath,
          });
          return;
        }
      }

      const controller = new AbortController();
      const insertionRef = insertionRange ? editor.api.rangeRef(insertionRange) : null;
      let currentFileIndex = 0;
      let restoredInsertion = false;
      let failed = false;
      uploadControllerRef.current = controller;
      uploadInFlightRef.current = true;
      setUploadFailure(null);
      setUploadingImage(true);
      onUploadingChangeRef.current?.(true);

      try {
        for (const [index, file] of files.entries()) {
          currentFileIndex = index;
          setUploadingName(`Checking ${file.name}`);
          const prepared = await prepareEditorImage(file);
          setUploadingName(
            prepared.optimized ? `Uploading optimized ${file.name}` : file.name,
          );
          const asset = await uploadAsset(vault, prepared.file, controller.signal);
          const assetId = assetIdFromUrl(asset.url);
          if (!assetId || assetId !== asset.id) {
            throw new Error("The image upload returned an invalid asset URL.");
          }
          unclaimedAssetIdsRef.current.add(assetId);

          if (controller.signal.aborted || !mountedRef.current) {
            discardIfUnclaimed(asset.url);
            throw new DOMException("Upload cancelled", "AbortError");
          }

          if (!restoredInsertion && insertionRef?.current) {
            editor.tf.select(insertionRef.current);
          }
          restoredInsertion = true;
          const alt = file.name.replace(/\.[^.]+$/, "") || "Image";
          if (replacementPath && index === 0) {
            const target = editor.api.node(replacementPath)?.[0] as {
              type?: string;
              url?: string;
            } | undefined;
            if (target?.type !== ImagePlugin.key) {
              throw new Error("The image to replace is no longer available.");
            }
            const replacedUrl = target.url;
            editor.tf.setNodes(
              { url: asset.url, caption: [{ text: alt }] },
              { at: replacementPath },
            );
            editor.tf.select(replacementPath);
            discardIfUnclaimed(replacedUrl);
          } else {
            editor.tf.insertNodes(
              {
                type: ImagePlugin.key,
                url: asset.url,
                caption: [{ text: alt }],
                children: [{ text: "" }],
              },
              { select: true },
            );
          }
        }
      } catch (error) {
        const aborted =
          controller.signal.aborted ||
          (error instanceof DOMException && error.name === "AbortError");
        failed = true;
        if (!aborted && mountedRef.current) {
          const failure = classifyEditorImageUploadFailure(
            error,
            files[currentFileIndex],
          );
          const deferred = deferredImageFilesRef.current.splice(0);
          setUploadFailure({
            files: failure.retryable
              ? [...files.slice(currentFileIndex), ...deferred]
              : [],
            message: failure.message,
            retryable: failure.retryable,
            kind: "error",
            replacementPath,
          });
        } else if (mountedRef.current && deferredImageFilesRef.current.length > 0) {
          setUploadFailure({
            files: deferredImageFilesRef.current.splice(0),
            message: "The current upload was cancelled before the next batch started.",
              retryable: true,
              kind: "error",
              replacementPath,
          });
        }
      } finally {
        insertionRef?.unref();
        if (uploadControllerRef.current === controller) {
          uploadControllerRef.current = null;
        }
        uploadInFlightRef.current = false;
        if (mountedRef.current) {
          setUploadingImage(false);
          setUploadingName("");
          onUploadingChangeRef.current?.(false);
          if (!failed && deferredImageFilesRef.current.length > 0) {
            setUploadFailure({
              files: deferredImageFilesRef.current.splice(0),
              message: "The previous image batch finished. Upload the next batch when ready.",
              retryable: true,
              kind: "queued",
            });
          }
        }
      }
    },
    [discardIfUnclaimed, editor, readOnly, vault],
  );

  const assetLifecycle = React.useMemo(
    () => ({
      vault,
      document,
      commit,
      readOnly,
      focusEditor: () => editorSurfaceRef.current?.focus(),
      requestImageReplacement: (path: number[]) => {
        replacementPathRef.current = path;
        imageInputRef.current?.click();
      },
    }),
    [commit, document, readOnly, vault],
  );

  const handleImageDragOver = React.useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      const hasImage =
        transferredImages(event.dataTransfer).length > 0 ||
        Array.from(event.dataTransfer.items).some(
          (item) => item.kind === "file" && item.type.startsWith("image/"),
        );
      if (!hasImage) return;

      // The browser opens a dropped local file when no element accepts it.
      // Consume image drags across the whole editor surface — including the
      // toolbar and upload banners — so an imprecise drop never replaces the
      // current page and destroys the draft.
      event.preventDefault();
      event.dataTransfer.dropEffect = readOnly ? "none" : "copy";
    },
    [readOnly],
  );

  const handleImageDrop = React.useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      const files = transferredImages(event.dataTransfer);
      if (files.length === 0) return;

      event.preventDefault();
      event.stopPropagation();
      if (readOnly) return;
      if (uploadInFlightRef.current) {
        deferredImageFilesRef.current.push(...files);
        return;
      }

      const droppedInBody =
        event.target instanceof Element &&
        event.target.closest("[contenteditable='true']") !== null;
      let dropRange = editor.selection;
      if (droppedInBody) {
        try {
          dropRange = editor.api.findEventRange(event.nativeEvent) || editor.selection;
        } catch {
          // Some browsers do not expose a caret coordinate for drops near a
          // void node or padding. Preserve the user's current caret instead
          // of allowing Slate to throw and tear down the authoring surface.
        }
      }
      void uploadImages(files, dropRange);
    },
    [editor, readOnly, uploadImages],
  );

  return (
    <EditorAssetLifecycleContext.Provider value={assetLifecycle}>
      <div onDragOverCapture={handleImageDragOver} onDropCapture={handleImageDrop}>
        <Plate
          editor={editor}
          onChange={({ editor: ed }) => {
            if (!onChange) return;
            // Serialize on every change — for documents in the typical AKB size
            // (single-digit KB markdown), this is well under a millisecond. Move
            // to a debounce only if profiling shows the cost.
            const md = ed.getApi(MarkdownPlugin).markdown.serialize({
              value: valueWithoutEditorTrailingBlock(ed.children) as typeof ed.children,
            });
            const assetIds: string[] = [];
            const visit = (nodes: unknown[]) => {
              for (const node of nodes) {
                if (!node || typeof node !== "object") continue;
                const element = node as {
                  type?: string;
                  url?: string;
                  children?: unknown[];
                };
                if (element.type === ImagePlugin.key) {
                  const assetId = assetIdFromUrl(element.url);
                  if (assetId) assetIds.push(assetId);
                }
                if (Array.isArray(element.children)) visit(element.children);
              }
            };
            visit(ed.children);
            onChange(md, assetIds);
          }}
      >
      {!readOnly && (
        <EditorToolbar
          uploadingImage={uploadingImage}
          onChooseImages={(files) => {
            const replacementPath = replacementPathRef.current ?? undefined;
            replacementPathRef.current = null;
            void uploadImages(files, editor.selection, replacementPath);
          }}
          onOpenImagePicker={() => {
            replacementPathRef.current = null;
            imageInputRef.current?.click();
          }}
          appearance={appearance}
          imageInputRef={imageInputRef}
        />
      )}
      {uploadingImage && (
        <Alert variant="info" title="Uploading image" className="border-x border-t-0">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="truncate">{uploadingName}</span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => uploadControllerRef.current?.abort()}
            >
              <X className="h-3.5 w-3.5" aria-hidden />
              Cancel upload
            </Button>
          </div>
        </Alert>
      )}
      {!uploadingImage && uploadFailure && (
        <Alert
          variant={uploadFailure.kind === "queued" ? "warning" : "destructive"}
          title={uploadFailure.kind === "queued" ? "Images waiting to upload" : "Image upload failed"}
          className="border-x border-t-0"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span className="min-w-0 flex-1">
              {uploadFailure.message}
              {uploadFailure.files.length > 1
                ? ` ${uploadFailure.files.length} images remain in this batch.`
                : ""}
            </span>
            <div className="flex flex-wrap items-center gap-1.5">
              {uploadFailure.retryable && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    void uploadImages(
                      uploadFailure.files,
                      editor.selection,
                      uploadFailure.replacementPath,
                    )
                  }
                >
                  <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                  {uploadFailure.kind === "queued" ? "Upload" : "Retry"}
                </Button>
              )}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  replacementPathRef.current = uploadFailure.replacementPath ?? null;
                  imageInputRef.current?.click();
                }}
              >
                <ImagePlus className="h-3.5 w-3.5" aria-hidden />
                Choose another
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setUploadFailure(null)}
              >
                Dismiss
              </Button>
            </div>
          </div>
        </Alert>
      )}
      <PlateContent
        ref={(node) => {
          editorSurfaceRef.current = node as HTMLDivElement | null;
        }}
        autoFocus={autoFocus}
        readOnly={readOnly}
        placeholder={placeholder}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledby}
        aria-required={required || undefined}
        onPasteCapture={(event) => {
          if (readOnly) return;
          const files = transferredImages(event.clipboardData);
          if (files.length === 0) return;
          if (!isStandaloneImageClipboard(event.clipboardData)) return;
          event.preventDefault();
          if (uploadInFlightRef.current) {
            deferredImageFilesRef.current.push(...files);
            return;
          }
          void uploadImages(files);
        }}
        className={cn(
          "!min-h-96 w-full outline-none cursor-text",
          // `prose` defaults to max-width: 65ch — explicitly override so
          // the editor expands to its container in Edit mode (typography
          // plugin's selector beats a plain `max-w-none`).
          "prose dark:prose-invert !max-w-none",
          "font-sans text-[15px] leading-7 text-foreground",
          // PlateContent renders a div whose direct children are blocks. The
          // default appearance keeps the reusable textarea-like frame; the
          // composer canvas deliberately delegates focus feedback to its
          // section label so a cursor never produces a second ghost rectangle.
          appearance === "canvas"
            ? "border-0 bg-transparent px-5 py-6 focus:outline-none focus-visible:outline-none focus-visible:ring-0 focus-visible:ring-offset-0 focus-within:outline-none focus-within:ring-0 focus-within:ring-offset-0 sm:px-8 lg:px-10"
            : appearance === "workspace"
              ? "border-0 bg-transparent px-4 py-4 focus:outline-none focus-visible:outline-none focus-visible:ring-0 focus-visible:ring-offset-0 focus-within:outline-none focus-within:ring-0 focus-within:ring-offset-0"
              : "border border-border bg-surface px-5 py-4 hover:border-foreground-muted focus-within:border-primary focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2 focus-within:ring-offset-background transition-colors",
          // Plate marks the first empty leaf with `data-slate-placeholder`
          // when the editor is empty; surface it so a blank editor isn't a
          // mysterious silent box.
          "[&_[data-slate-placeholder=true]]:text-foreground-muted [&_[data-slate-placeholder=true]]:italic",
          className,
        )}
      />
        </Plate>
      </div>
    </EditorAssetLifecycleContext.Provider>
  );
}

export default MarkdownEditor;
