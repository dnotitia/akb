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
  ImagePlus,
  Loader2,
  Pilcrow,
  RotateCcw,
  Trash2,
  X,
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
import { ApiError, discardAsset, uploadAsset } from "@/lib/api";
import {
  assetIdFromUrl,
  EDITOR_IMAGE_MIME_TYPES,
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

function ListItemElement(props: PlateElementProps) {
  return <PlateElement {...props} as="li" className="leading-7" />;
}

function TableElement(props: PlateElementProps) {
  // Wide tables scroll within themselves instead of pushing the whole
  // authoring surface into a page-level horizontal scroll.
  return (
    <PlateElement
      {...props}
      as="table"
      className="my-4 w-full border border-border text-sm block overflow-x-auto max-w-full"
    />
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
  return (
    <PlateElement {...props} className="my-4">
      <figure contentEditable={false} className="group relative m-0">
        <AssetImage
          src={element.url}
          alt={alt}
          assetContext={
            assetLifecycle
              ? { mode: "authenticated", vault: assetLifecycle.vault }
              : undefined
          }
          className="my-0 max-h-[70vh] object-contain"
        />
        {alt && (
          <figcaption className="mt-1.5 text-center text-xs text-foreground-muted">
            {alt}
          </figcaption>
        )}
        <Button
          type="button"
          variant="destructive"
          size="icon"
          aria-label={alt ? `Remove image: ${alt}` : "Remove image"}
          title="Remove image"
          className="absolute right-2 top-2 h-8 w-8 opacity-0 shadow-sm group-hover:opacity-100 group-focus-within:opacity-100 focus:opacity-100"
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            const path = editor.api.findPath(props.element);
            if (path) {
              editor.tf.removeNodes({ at: path });
            }
          }}
        >
          <Trash2 className="h-4 w-4" aria-hidden />
        </Button>
      </figure>
      {props.children}
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
  TablePlugin,
  TableRowPlugin,
  TableCellPlugin,
  TableCellHeaderPlugin,
  ImagePlugin,
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

interface EditorToolbarProps {
  uploadingImage: boolean;
  onChooseImages: (files: File[]) => void;
}

function EditorToolbar({ uploadingImage, onChooseImages }: EditorToolbarProps) {
  // useEditorState re-renders on editor changes so active states stay in sync;
  // useEditorRef gives a stable handle for the mutating callbacks.
  const editor = useEditorRef();
  const state = useEditorState();
  const imageInputRef = React.useRef<HTMLInputElement>(null);

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
      role="toolbar"
      aria-label="Text formatting"
      className={cn(
        "sticky top-0 z-10 flex flex-wrap items-center gap-1.5",
        "border-b border-border bg-surface px-2 py-1.5",
        "rounded-t-[var(--radius-sm)] select-none",
      )}
    >
      {/* Block types */}
      <div className={TOOLBAR_GROUP} role="group" aria-label="Block type">
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
      <div className={TOOLBAR_GROUP} role="group" aria-label="Marks">
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
      <div className={TOOLBAR_GROUP} role="group" aria-label="Lists">
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
      <div className={TOOLBAR_GROUP} role="group" aria-label="Blocks">
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
      <div className={TOOLBAR_GROUP} role="group" aria-label="Insert">
        <RibbonButton label="Insert link on selection" onClick={onLink}>
          <Link2 className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton label="Insert table" onClick={onTable}>
          <TableIcon className="h-4 w-4" />
        </RibbonButton>
        <RibbonButton
          label={uploadingImage ? "Uploading image" : "Insert image"}
          disabled={uploadingImage}
          onClick={() => imageInputRef.current?.click()}
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
      <div className={TOOLBAR_GROUP} role="group" aria-label="History">
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
  /** Accessible name for the contenteditable region (it carries role=textbox
   * but no native label). Pass one of these so SR users hear the field name. */
  ariaLabel?: string;
  ariaLabelledby?: string;
  required?: boolean;
  /** Vault receiving pasted, dropped, or picked image assets. */
  vault: string;
  /** Lets the owning form block save/navigation while an upload is in flight. */
  onUploadingChange?: (uploading: boolean) => void;
  /** Keep uploaded assets for an in-flight document save to claim. The server
   * GC remains the fallback when the save ultimately fails after navigation. */
  preserveUploadsOnUnmount?: boolean;
}

export function MarkdownEditor({
  value,
  onChange,
  placeholder = "Write in markdown — slash commands and shortcuts work.",
  autoFocus,
  readOnly,
  className,
  ariaLabel,
  ariaLabelledby,
  required,
  vault,
  onUploadingChange,
  preserveUploadsOnUnmount = false,
}: MarkdownEditorProps) {
  const [uploadingImage, setUploadingImage] = React.useState(false);
  const [uploadingName, setUploadingName] = React.useState("");
  const [uploadFailure, setUploadFailure] = React.useState<{
    files: File[];
    message: string;
    retryable: boolean;
  } | null>(null);
  const uploadControllerRef = React.useRef<AbortController | null>(null);
  const uploadInFlightRef = React.useRef(false);
  const deferredImageFilesRef = React.useRef<File[]>([]);
  const unclaimedAssetIdsRef = React.useRef(new Set<string>());
  const mountedRef = React.useRef(true);
  const onUploadingChangeRef = React.useRef(onUploadingChange);
  const preserveUploadsOnUnmountRef = React.useRef(preserveUploadsOnUnmount);

  const discardIfUnclaimed = React.useCallback(
    (url: string | undefined) => {
      const assetId = assetIdFromUrl(url);
      if (!assetId || !unclaimedAssetIdsRef.current.has(assetId)) return;
      void discardAsset(vault, assetId)
        .then(() => unclaimedAssetIdsRef.current.delete(assetId))
        .catch(() => {
          // Retain the id for the unmount retry. A future server-side TTL
          // sweep remains the final backstop for abrupt browser termination.
        });
    },
    [vault],
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
          void discardAsset(vault, assetId)
            .then(() => unclaimedAssetIds.delete(assetId))
            .catch(() => undefined);
        }
      }
      onUploadingChangeRef.current?.(false);
    };
  }, [vault]);

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

  const uploadImages = React.useCallback(
    async (files: File[], insertionRange = editor.selection) => {
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
          setUploadingName(file.name);
          const asset = await uploadAsset(vault, file, controller.signal);
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
      } catch (error) {
        const aborted =
          controller.signal.aborted ||
          (error instanceof DOMException && error.name === "AbortError");
        failed = true;
        if (!aborted && mountedRef.current) {
          const message =
            error instanceof ApiError || error instanceof Error
              ? error.message
              : "The image could not be uploaded.";
          const deferred = deferredImageFilesRef.current.splice(0);
          setUploadFailure({
            files: [...files.slice(currentFileIndex), ...deferred],
            message,
            retryable: true,
          });
        } else if (mountedRef.current && deferredImageFilesRef.current.length > 0) {
          setUploadFailure({
            files: deferredImageFilesRef.current.splice(0),
            message: "The current upload was cancelled before the next batch started.",
            retryable: true,
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
            });
          }
        }
      }
    },
    [discardIfUnclaimed, editor, readOnly, vault],
  );

  const assetLifecycle = React.useMemo(
    () => ({ vault }),
    [vault],
  );

  return (
    <EditorAssetLifecycleContext.Provider value={assetLifecycle}>
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
      {!readOnly && (
        <EditorToolbar
          uploadingImage={uploadingImage}
          onChooseImages={(files) => void uploadImages(files)}
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
        <Alert variant="destructive" title="Image upload failed" className="border-x border-t-0">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span>
              {uploadFailure.message}
              {uploadFailure.files.length > 1
                ? ` ${uploadFailure.files.length} images remain in this batch.`
                : ""}
            </span>
            {uploadFailure.retryable ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => void uploadImages(uploadFailure.files)}
              >
                <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                Retry
              </Button>
            ) : (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setUploadFailure(null)}
              >
                Dismiss
              </Button>
            )}
          </div>
        </Alert>
      )}
      <PlateContent
        autoFocus={autoFocus}
        readOnly={readOnly}
        placeholder={placeholder}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledby}
        aria-required={required || undefined}
        onPaste={(event) => {
          if (readOnly) return;
          const files = transferredImages(event.clipboardData);
          if (files.length === 0) return;
          const carriesDocumentContent = ["text/plain", "text/html"].some(
            (type) => event.clipboardData.getData(type).trim().length > 0,
          );
          if (carriesDocumentContent) return;
          event.preventDefault();
          if (uploadInFlightRef.current) {
            deferredImageFilesRef.current.push(...files);
            return;
          }
          void uploadImages(files);
        }}
        onDragOver={(event) => {
          if (
            !readOnly &&
            Array.from(event.dataTransfer.items).some(
              (item) => item.kind === "file" && item.type.startsWith("image/"),
            )
          ) {
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
          }
        }}
        onDrop={(event) => {
          if (readOnly) return;
          const files = transferredImages(event.dataTransfer);
          if (files.length === 0) return;
          // Always consume an image-file drop once drag-over advertised copy.
          // Letting the browser's default run here can navigate to/open the
          // local file, which would discard the editor session. A concurrent
          // batch is retained in the visible retry affordance instead.
          event.preventDefault();
          event.stopPropagation();
          if (uploadInFlightRef.current) {
            deferredImageFilesRef.current.push(...files);
            return;
          }
          const dropRange = editor.api.findEventRange(event.nativeEvent) || editor.selection;
          void uploadImages(files, dropRange);
        }}
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
          "hover:border-foreground-muted focus-within:border-primary focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2 focus-within:ring-offset-background transition-colors",
          // Plate marks the first empty leaf with `data-slate-placeholder`
          // when the editor is empty; surface it so a blank editor isn't a
          // mysterious silent box.
          "[&_[data-slate-placeholder=true]]:text-foreground-muted [&_[data-slate-placeholder=true]]:italic",
          className,
        )}
      />
      </Plate>
    </EditorAssetLifecycleContext.Provider>
  );
}

export default MarkdownEditor;
