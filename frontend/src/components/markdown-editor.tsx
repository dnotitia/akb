import * as React from "react";
import { createPortal } from "react-dom";
import {
  EditorContent,
  MarkdownEditingSurface,
  MarkdownToolbar,
  MarkdownToolbarButton,
  MarkdownToolbarGroup,
  useMarkdownCommands,
  useMarkdownEditor,
  useMarkdownTargetResolutions,
  type MarkdownLinkSearchLabels,
} from "@akb/markdown-editor/react";
import {
  extractMarkdownTargets,
  serializeEditorMarkdown,
} from "@akb/markdown-editor";
import {
  ImagePlus,
  Loader2,
  Pencil,
  Replace,
  RotateCcw,
  X,
} from "lucide-react";
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
import { discardAsset, getAssetBlob } from "@/lib/api";
import { normalizeEditorLinkUrl } from "@/lib/editor-link";
import {
  canonicalAkbMarkdownTarget,
  classifyAkbMarkdownTarget,
  createAkbMarkdownAdapters,
  type AkbMarkdownTargetResolution,
} from "@/lib/markdown-adapters";
import {
  assetIdFromUrl,
  classifyEditorImageUploadFailure,
  EDITOR_IMAGE_MIME_TYPES,
  prepareEditorImage,
  validateEditorImage,
} from "@/lib/image-assets";
import { cn } from "@/lib/utils";

type MarkdownEditorInstance = NonNullable<ReturnType<typeof useMarkdownEditor>>;

type EditorCommand = (...args: unknown[]) => boolean;

function invokeCommand(
  editor: MarkdownEditorInstance,
  name: string,
  ...args: unknown[]
): boolean {
  const command = (editor.commands as unknown as Record<string, EditorCommand>)[
    name
  ];
  return command ? Reflect.apply(command, editor.commands, args) : false;
}

function imageAssetIds(editor: MarkdownEditorInstance): string[] {
  const ids = new Set<string>();

  const visit = (node: {
    type?: string;
    attrs?: { target?: unknown };
    content?: unknown[];
  }) => {
    if (node.type === "image" && typeof node.attrs?.target === "string") {
      const id = assetIdFromUrl(node.attrs.target);
      if (id) ids.add(id);
    }
    for (const child of node.content ?? []) {
      if (child && typeof child === "object") {
        visit(
          child as {
            type?: string;
            attrs?: { target?: unknown };
            content?: unknown[];
          },
        );
      }
    }
  };

  visit(editor.getJSON());
  return [...ids];
}

function imageAssetIdsFromMarkdown(markdown: string): string[] {
  const ids = new Set<string>();
  for (const target of extractMarkdownTargets(markdown)) {
    if (target.kind !== "attachment") continue;
    const id = assetIdFromUrl(target.target);
    if (id) ids.add(id);
  }
  return [...ids];
}

function ensureTrailingParagraph(editor: MarkdownEditorInstance): void {
  const last = editor.state.doc.lastChild;
  if (!last || last.type.name !== "paragraph") {
    try {
      editor.commands.insertContentAt(editor.state.doc.content.size, {
        type: "paragraph",
      });
    } catch {
      // The shared image schema accepts a legacy top-level image node. If that
      // parsed document cannot accept a trailing block, preserve its source
      // instead of tearing down the editor; the next user edit can still be
      // serialized through the common core.
    }
  }
}

function normalizeTopLevelImages(editor: MarkdownEditorInstance): void {
  const document = editor.getJSON();
  if (!document.content?.some((node) => node.type === "image")) return;
  const content = document.content.map((node) =>
    node.type === "image" ? { type: "paragraph", content: [node] } : node,
  );
  editor.commands.setContent({ ...document, content });
}

function editorContentElement(root: HTMLDivElement | null): HTMLElement | null {
  return root?.querySelector<HTMLElement>(".ProseMirror") ?? null;
}

function applyMarkdownTargetResolutions(
  root: HTMLElement,
  resolutions: ReadonlyMap<string, AkbMarkdownTargetResolution>,
  resolving: boolean,
): void {
  const setAttribute = (element: HTMLElement, name: string, value: string) => {
    if (element.getAttribute(name) !== value) element.setAttribute(name, value);
  };
  const removeAttribute = (element: HTMLElement, name: string) => {
    if (element.hasAttribute(name)) element.removeAttribute(name);
  };
  root
    .querySelectorAll<HTMLElement>("img[data-markdown-target], a[href]")
    .forEach((element) => {
      const rawTarget =
        element.dataset.markdownTarget ??
        (element.tagName === "A" ? element.getAttribute("href") : null);
      if (!rawTarget) return;

      const target = canonicalAkbMarkdownTarget(rawTarget);
      const kind = target ? classifyAkbMarkdownTarget(target) : null;
      if (!target || !kind) return;

      setAttribute(element, "data-markdown-target", target);
      // Private attachment bytes are loaded through the authenticated adapter
      // below. Never replace that source with an unauthenticated browser URL.
      if (element.tagName === "IMG" && kind === "attachment") return;

      const resolution = resolutions.get(target);
      if (!resolution) {
        if (!resolving) return;
        setAttribute(element, "data-markdown-resolution", "pending");
        setAttribute(element, "aria-disabled", "true");
        if (element.tagName === "A") setAttribute(element, "href", "#");
        return;
      }

      if (resolution.status === "available" && resolution.runtimeUrl) {
        if (element.tagName === "IMG")
          setAttribute(element, "src", resolution.runtimeUrl);
        else setAttribute(element, "href", resolution.runtimeUrl);
        setAttribute(element, "data-markdown-resolution", "available");
        removeAttribute(element, "aria-disabled");
        removeAttribute(element, "aria-label");
        removeAttribute(element, "title");
        return;
      }

      setAttribute(element, "data-markdown-resolution", "unavailable");
      if (element.tagName === "A") {
        setAttribute(
          element,
          "title",
          resolution.label ?? "Reference unavailable",
        );
        removeAttribute(element, "aria-label");
      } else
        setAttribute(
          element,
          "aria-label",
          resolution.label ?? "Reference unavailable",
        );
      setAttribute(element, "aria-disabled", "true");
      if (element.tagName === "A") setAttribute(element, "href", "#");
    });
}

function useTargetResolutionDom(
  rootRef: React.RefObject<HTMLDivElement | null>,
  editor: MarkdownEditorInstance | null,
  resolutions: ReadonlyMap<string, AkbMarkdownTargetResolution>,
  resolving: boolean,
): void {
  React.useLayoutEffect(() => {
    if (!editor) return;
    const root = editor.view.dom as HTMLElement;

    const apply = () => {
      applyMarkdownTargetResolutions(root, resolutions, resolving);
    };
    apply();
    let retryCount = 0;
    let timer: number | null = null;
    const retry = () => {
      apply();
      retryCount += 1;
      if (retryCount < 8) timer = window.setTimeout(retry, 10);
    };
    retry();
    editor.on("transaction", apply);
    const observer =
      typeof MutationObserver === "undefined"
        ? null
        : new MutationObserver(apply);
    observer?.observe(root, {
      attributes: true,
      attributeFilter: ["href", "aria-disabled", "data-markdown-resolution"],
      childList: true,
      subtree: true,
    });
    return () => {
      editor.off("transaction", apply);
      observer?.disconnect();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [editor, resolutions, resolving, rootRef]);
}

/** Load private assets with the authenticated API while leaving Markdown's
 * canonical `/api/assets/<uuid>` target unchanged in the editor model. */
function usePrivateImageSources(
  rootRef: React.RefObject<HTMLDivElement | null>,
  editor: MarkdownEditorInstance | null,
  vault: string,
  document?: string,
  commit?: string,
): void {
  const entriesRef = React.useRef(
    new Map<string, { controller: AbortController; url?: string }>(),
  );

  React.useEffect(() => {
    if (!editor) return;
    const root = editor.view.dom as HTMLElement;

    const contextKey = `${vault}\u0000${document ?? ""}\u0000${commit ?? ""}`;
    const sync = () => {
      const images = [
        ...root.querySelectorAll<HTMLImageElement>("img[data-markdown-target]"),
      ];
      const seen = new Set<string>();

      for (const image of images) {
        const target = canonicalAkbMarkdownTarget(
          image.dataset.markdownTarget ?? "",
          "attachment",
        );
        const assetId = target ? assetIdFromUrl(target) : null;
        if (!target || !assetId) continue;

        const key = `${contextKey}\u0000${assetId}`;
        seen.add(key);
        const existing = entriesRef.current.get(key);
        if (existing) {
          if (existing.url) image.src = existing.url;
          continue;
        }

        const controller = new AbortController();
        entriesRef.current.set(key, { controller });
        void getAssetBlob(
          assetId,
          vault,
          controller.signal,
          document && commit ? { document, commit } : undefined,
        )
          .then((blob) => {
            if (controller.signal.aborted) return;
            const url =
              typeof URL.createObjectURL === "function"
                ? URL.createObjectURL(blob)
                : target;
            entriesRef.current.set(key, { controller, url });
            root
              .querySelectorAll<HTMLImageElement>(
                `img[data-markdown-target="${target}"]`,
              )
              .forEach((element) => {
                element.src = url;
                element.dataset.markdownResolution = "available";
              });
          })
          .catch(() => {
            if (controller.signal.aborted) return;
            root
              .querySelectorAll<HTMLImageElement>(
                `img[data-markdown-target="${target}"]`,
              )
              .forEach((element) => {
                element.removeAttribute("src");
                element.dataset.markdownResolution = "unavailable";
                element.setAttribute("aria-label", "Image unavailable");
              });
            entriesRef.current.set(key, { controller });
          });
      }

      for (const [key, entry] of entriesRef.current) {
        if (seen.has(key)) continue;
        entry.controller.abort();
        if (
          entry.url?.startsWith("blob:") &&
          typeof URL.revokeObjectURL === "function"
        ) {
          URL.revokeObjectURL(entry.url);
        }
        entriesRef.current.delete(key);
      }
    };

    sync();
    editor.on("transaction", sync);
    const observer =
      typeof MutationObserver === "undefined"
        ? null
        : new MutationObserver(sync);
    observer?.observe(root, { childList: true, subtree: true });
    const entries = entriesRef.current;

    return () => {
      editor.off("transaction", sync);
      observer?.disconnect();
      for (const entry of entries.values()) {
        entry.controller.abort();
        if (
          entry.url?.startsWith("blob:") &&
          typeof URL.revokeObjectURL === "function"
        ) {
          URL.revokeObjectURL(entry.url);
        }
      }
      entries.clear();
    };
  }, [commit, document, editor, rootRef, vault]);
}

interface ImageHost {
  image: HTMLImageElement;
  host: HTMLDivElement;
  target: string;
  alt: string;
}

function useImageHosts(
  rootRef: React.RefObject<HTMLDivElement | null>,
  editor: MarkdownEditorInstance | null,
  readOnly: boolean,
): ImageHost[] {
  const [hosts, setHosts] = React.useState<ImageHost[]>([]);
  const hostsRef = React.useRef<ImageHost[]>([]);

  React.useLayoutEffect(() => {
    const hostRoot = rootRef.current;
    if (!hostRoot || !editor || readOnly) {
      for (const current of hostsRef.current) current.host.remove();
      hostsRef.current = [];
      setHosts([]);
      return;
    }
    const root = editor.view.dom as HTMLElement;

    const sync = () => {
      const next: ImageHost[] = [];
      root
        .querySelectorAll<HTMLImageElement>("img[data-markdown-target]")
        .forEach((image) => {
          const target = canonicalAkbMarkdownTarget(
            image.dataset.markdownTarget ?? "",
          );
          if (!target) return;
          let host = [...hostRoot.children].find(
            (child) =>
              child instanceof HTMLDivElement &&
              child.dataset.markdownImageTarget === target,
          ) as HTMLDivElement | undefined;
          if (!host) {
            host = window.document.createElement("div");
            host.dataset.markdownImageControls = "true";
            host.dataset.markdownImageTarget = target;
            host.contentEditable = "false";
            host.className =
              "absolute right-2 top-2 flex items-center gap-1 rounded-[var(--radius-md)] border border-border bg-surface/90 p-1 shadow-sm backdrop-blur-sm";
            hostRoot.append(host);
          }
          const rootRect = hostRoot.getBoundingClientRect();
          const imageRect = image.getBoundingClientRect();
          host.style.top = `${Math.max(0, imageRect.top - rootRect.top + 4)}px`;
          host.style.left = `${Math.max(0, imageRect.right - rootRect.left - 112)}px`;
          next.push({
            image,
            host,
            target,
            alt: image.getAttribute("alt") ?? "",
          });
        });

      const nextHosts = new Set(next.map((entry) => entry.host));
      for (const current of hostsRef.current) {
        if (!nextHosts.has(current.host)) current.host.remove();
      }
      const unchanged =
        next.length === hostsRef.current.length &&
        next.every(
          (entry, index) =>
            entry.image === hostsRef.current[index]?.image &&
            entry.alt === hostsRef.current[index]?.alt,
        );
      hostsRef.current = next;
      if (!unchanged) setHosts(next);
    };

    sync();
    editor.on("transaction", sync);
    const observer =
      typeof MutationObserver === "undefined"
        ? null
        : new MutationObserver(sync);
    observer?.observe(root, {
      childList: true,
      subtree: true,
      characterData: true,
    });

    return () => {
      editor.off("transaction", sync);
      observer?.disconnect();
      for (const current of hostsRef.current) current.host.remove();
      hostsRef.current = [];
    };
  }, [editor, readOnly, rootRef]);

  return hosts;
}

function imagePosition(
  editor: MarkdownEditorInstance,
  image: HTMLImageElement,
): number | null {
  try {
    return editor.view.posAtDOM(image, 0);
  } catch {
    return null;
  }
}

function ImageControls({
  editor,
  image,
  alt,
  onReplace,
}: {
  editor: MarkdownEditorInstance;
  image: HTMLImageElement;
  target: string;
  alt: string;
  onReplace: (position: number) => void;
}) {
  const [descriptionOpen, setDescriptionOpen] = React.useState(false);
  const [description, setDescription] = React.useState(alt);
  const [descriptionError, setDescriptionError] = React.useState("");
  const descriptionId = React.useId();

  React.useEffect(() => setDescription(alt), [alt]);

  const selectImage = (): number | null => {
    const position = imagePosition(editor, image);
    if (position === null) return null;
    invokeCommand(editor, "setNodeSelection", position);
    return position;
  };

  const removeImage = () => {
    if (selectImage() === null) return;
    invokeCommand(editor, "focus");
    invokeCommand(editor, "deleteSelection");
    ensureTrailingParagraph(editor);
  };

  const saveDescription = () => {
    const next = description.trim();
    if (!next) {
      setDescriptionError(
        "Describe the image so it remains understandable without sight.",
      );
      return;
    }
    const position = selectImage();
    if (position === null) return;
    invokeCommand(editor, "focus");
    invokeCommand(editor, "updateAttributes", "image", { alt: next });
    setDescriptionError("");
    setDescriptionOpen(false);
  };

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label={
          alt ? `Edit image description: ${alt}` : "Edit image description"
        }
        title="Edit image description"
        className="h-7 w-7 text-foreground-muted hover:bg-surface-hover hover:text-foreground"
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => {
          setDescription(alt);
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
        onClick={() => {
          const position = imagePosition(editor, image);
          if (position !== null) onReplace(position);
        }}
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
      <Dialog open={descriptionOpen} onOpenChange={setDescriptionOpen}>
        <DialogContent
          className="sm:max-w-md"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (!editor.isDestroyed) editor.commands.focus();
          }}
        >
          <DialogHeader>
            <DialogTitle>Image description</DialogTitle>
            <DialogDescription>
              This text is used as the image alt text and visible caption.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor={descriptionId}>Description</Label>
            <Input
              id={descriptionId}
              value={description}
              onChange={(event) => {
                setDescription(event.target.value);
                if (descriptionError) setDescriptionError("");
              }}
              aria-invalid={descriptionError ? true : undefined}
              aria-describedby={
                descriptionError ? `${descriptionId}-error` : undefined
              }
              autoFocus
            />
            {descriptionError && (
              <p
                id={`${descriptionId}-error`}
                role="alert"
                className="text-xs text-destructive"
              >
                {descriptionError}
              </p>
            )}
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setDescriptionOpen(false)}
            >
              Cancel
            </Button>
            <Button type="button" onClick={saveDescription}>
              Save description
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

interface EditorToolbarProps {
  editor: MarkdownEditorInstance | null;
  searchAdapter?: ReturnType<typeof createAkbMarkdownAdapters>["search"];
  vault: string;
  uploadingImage: boolean;
  onChooseImages: (files: File[]) => void;
  onOpenImagePicker: () => void;
  appearance: "framed" | "canvas" | "workspace";
  imageInputRef: React.RefObject<HTMLInputElement | null>;
}

const AKB_MARKDOWN_TABLE_OPTIONS = {
  labels: {
    editableTable: "Editable table",
    readOnlyTable: "Table",
    actions: "Table actions",
    insertTable: "Insert table",
    addRow: "Add row after selected row",
    addColumn: "Add column right of selected column",
    removeRow: "Remove selected row",
    removeColumn: "Remove selected column",
    continueBelow: "Continue below",
    deleteTable: "Delete table",
  },
  tableClassName: "!table",
};

function EditorToolbar({
  editor,
  searchAdapter,
  vault,
  uploadingImage,
  onChooseImages,
  onOpenImagePicker,
  appearance,
  imageInputRef,
}: EditorToolbarProps) {
  return (
    <MarkdownToolbar
      editor={editor}
      link={{
        normalizeUrl: normalizeEditorLinkUrl,
        searchAdapter,
        searchContext: { vault },
        searchLabels: AKB_MARKDOWN_SEARCH_LABELS,
      }}
      table={AKB_MARKDOWN_TABLE_OPTIONS}
      className={cn(
        appearance === "canvas"
          ? "bg-surface/95 px-5 py-2 backdrop-blur-sm sm:px-8 lg:px-10"
          : appearance === "workspace"
            ? "bg-surface px-3 py-2"
            : "rounded-t-[var(--radius-sm)] bg-surface px-2 py-1.5",
      )}
    >
      <MarkdownToolbarGroup label="Attachments">
        <MarkdownToolbarButton
          label={uploadingImage ? "Uploading image" : "Insert image"}
          disabled={!editor || uploadingImage}
          onClick={onOpenImagePicker}
        >
          {uploadingImage ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <ImagePlus className="h-4 w-4" />
          )}
        </MarkdownToolbarButton>
        <input
          ref={imageInputRef}
          type="file"
          accept={EDITOR_IMAGE_MIME_TYPES.join(",")}
          className="sr-only"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(event) => {
            const files = Array.from(event.currentTarget.files ?? []);
            event.currentTarget.value = "";
            if (files.length) onChooseImages(files);
          }}
        />
      </MarkdownToolbarGroup>
    </MarkdownToolbar>
  );
}

const AKB_MARKDOWN_SEARCH_LABELS: Partial<MarkdownLinkSearchLabels> = {
  inputLabel: "Search Vault resources",
  inputPlaceholder: "Find a document or file",
  searching: "Searching…",
  empty: "No matching documents or files found.",
  error: "Unable to search resources. Check your access and try again.",
  retry: "Retry search",
  results: "Vault resource results",
  document: "Document",
  file: "File",
  resource: "Resource",
};

function transferredImages(transfer: DataTransfer): File[] {
  return Array.from(transfer.files ?? []).filter((file) =>
    file.type.startsWith("image/"),
  );
}

function isStandaloneImageClipboard(transfer: DataTransfer): boolean {
  const files = transferredImages(transfer);
  if (!files.length) return false;
  const html = transfer.getData("text/html");
  if (!html.trim()) {
    const plain = transfer.getData("text/plain").trim();
    if (!plain) return true;
    const imageNames = new Set(files.map((file) => file.name));
    const lines = plain
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
    return lines.length > 0 && lines.every((line) => imageNames.has(line));
  }

  if (!/<img\b/i.test(html)) return false;
  const template = document.createElement("template");
  template.innerHTML = html;
  for (const node of template.content.querySelectorAll("img, meta, link, style")) {
    node.remove();
  }
  return !(template.content.textContent ?? "").trim();
}

export interface MarkdownEditorProps {
  value: string;
  onChange?: (markdown: string, assetIds: readonly string[]) => void;
  placeholder?: string;
  autoFocus?: boolean;
  readOnly?: boolean;
  className?: string;
  appearance?: "framed" | "canvas" | "workspace";
  ariaLabel?: string;
  ariaLabelledby?: string;
  required?: boolean;
  vault: string;
  document?: string;
  commit?: string;
  onUploadingChange?: (uploading: boolean) => void;
  preserveUploadsOnUnmount?: boolean;
  claimedAssetIds?: readonly string[] | null;
  initialUnclaimedAssetIds?: readonly string[];
  initialUnclaimedAssetExpirations?: Readonly<Record<string, string>>;
  onAssetExpirationsChange?: (
    expirations: Readonly<Record<string, string>>,
  ) => void;
  onUnclaimedAssetIdsChange?: (assetIds: readonly string[]) => void;
}

export function MarkdownEditor({
  value,
  onChange,
  placeholder = "Write in markdown — slash commands and shortcuts work.",
  autoFocus,
  readOnly = false,
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
  initialUnclaimedAssetExpirations = {},
  onAssetExpirationsChange,
  onUnclaimedAssetIdsChange,
}: MarkdownEditorProps) {
  const rootRef = React.useRef<HTMLDivElement>(null);
  const imageInputRef = React.useRef<HTMLInputElement>(null);
  const replacementPositionRef = React.useRef<number | null>(null);
  const uploadControllerRef = React.useRef<AbortController | null>(null);
  const uploadInFlightRef = React.useRef(false);
  const deferredImageFilesRef = React.useRef<File[]>([]);
  const unclaimedAssetIdsRef = React.useRef(new Set(initialUnclaimedAssetIds));
  const unclaimedAssetExpirationsRef = React.useRef(
    new Map(Object.entries(initialUnclaimedAssetExpirations)),
  );
  const discardingAssetIdsRef = React.useRef(new Set<string>());
  const mountedRef = React.useRef(true);
  const preserveUploadsOnUnmountRef = React.useRef(preserveUploadsOnUnmount);
  const onUploadingChangeRef = React.useRef(onUploadingChange);
  const onAssetExpirationsChangeRef = React.useRef(onAssetExpirationsChange);
  const onUnclaimedAssetIdsChangeRef = React.useRef(onUnclaimedAssetIdsChange);
  const [uploadingImage, setUploadingImage] = React.useState(false);
  const [uploadingName, setUploadingName] = React.useState("");
  const [uploadFailure, setUploadFailure] = React.useState<{
    files: File[];
    message: string;
    retryable: boolean;
    kind: "error" | "queued";
    replacementPosition?: number;
  } | null>(null);

  const adapters = React.useMemo(
    () => createAkbMarkdownAdapters({ vault, document, commit }),
    [commit, document, vault],
  );
  const targetResolutions = useMarkdownTargetResolutions(
    value,
    adapters.targetResolver,
    { vault, document, commit },
  );
  const handleChange = React.useCallback(
    (_: string, editor: MarkdownEditorInstance) => {
      onChange?.(
        serializeEditorMarkdown(editor, { profile: "preserve" }),
        imageAssetIds(editor),
      );
    },
    [onChange],
  );
  const handleSourceChange = React.useCallback(
    (markdown: string) => {
      onChange?.(markdown, imageAssetIdsFromMarkdown(markdown));
    },
    [onChange],
  );
  const editor = useMarkdownEditor({
    initialMarkdown: value,
    profile: "preserve",
    editable: !readOnly,
    onChange: handleChange,
  });
  const commands = useMarkdownCommands(editor);

  React.useEffect(() => {
    onUploadingChangeRef.current = onUploadingChange;
    onAssetExpirationsChangeRef.current = onAssetExpirationsChange;
    onUnclaimedAssetIdsChangeRef.current = onUnclaimedAssetIdsChange;
    preserveUploadsOnUnmountRef.current = preserveUploadsOnUnmount;
  }, [
    onAssetExpirationsChange,
    onUnclaimedAssetIdsChange,
    onUploadingChange,
    preserveUploadsOnUnmount,
  ]);

  const reportAssetExpirations = React.useCallback(() => {
    onAssetExpirationsChangeRef.current?.(
      Object.fromEntries(unclaimedAssetExpirationsRef.current.entries()),
    );
  }, []);
  const reportUnclaimedAssetIds = React.useCallback(() => {
    onUnclaimedAssetIdsChangeRef.current?.([...unclaimedAssetIdsRef.current]);
  }, []);
  const discardUnclaimedAsset = React.useCallback(
    (assetId: string) => {
      if (
        !unclaimedAssetIdsRef.current.has(assetId) ||
        discardingAssetIdsRef.current.has(assetId)
      )
        return;
      discardingAssetIdsRef.current.add(assetId);
      void discardAsset(vault, assetId)
        .then(() => {
          unclaimedAssetIdsRef.current.delete(assetId);
          unclaimedAssetExpirationsRef.current.delete(assetId);
          reportAssetExpirations();
          reportUnclaimedAssetIds();
        })
        .catch(() => undefined)
        .finally(() => discardingAssetIdsRef.current.delete(assetId));
    },
    [reportAssetExpirations, reportUnclaimedAssetIds, vault],
  );
  const discardIfUnclaimed = React.useCallback(
    (target: string | undefined) => {
      const assetId = assetIdFromUrl(target);
      if (assetId) discardUnclaimedAsset(assetId);
    },
    [discardUnclaimedAsset],
  );

  React.useEffect(() => {
    reportUnclaimedAssetIds();
  }, [reportUnclaimedAssetIds]);
  React.useEffect(() => {
    mountedRef.current = true;
    const unclaimedAssetIds = unclaimedAssetIdsRef.current;
    return () => {
      mountedRef.current = false;
      uploadControllerRef.current?.abort();
      if (!preserveUploadsOnUnmountRef.current) {
        for (const assetId of unclaimedAssetIds) discardUnclaimedAsset(assetId);
      }
      onUploadingChangeRef.current?.(false);
    };
  }, [discardUnclaimedAsset]);
  React.useLayoutEffect(() => {
    if (claimedAssetIds === null) return;
    const accepted = new Set(claimedAssetIds);
    for (const assetId of unclaimedAssetIdsRef.current) {
      if (accepted.has(assetId)) {
        unclaimedAssetIdsRef.current.delete(assetId);
        unclaimedAssetExpirationsRef.current.delete(assetId);
      } else discardUnclaimedAsset(assetId);
    }
    reportAssetExpirations();
    reportUnclaimedAssetIds();
  }, [
    claimedAssetIds,
    discardUnclaimedAsset,
    reportAssetExpirations,
    reportUnclaimedAssetIds,
  ]);

  React.useEffect(() => {
    if (!editor) return;
    normalizeTopLevelImages(editor);
    ensureTrailingParagraph(editor);
    if (autoFocus && !readOnly)
      requestAnimationFrame(() => editor.commands.focus());
  }, [autoFocus, editor, readOnly]);
  const handleMarkdownApplied = React.useCallback(
    (currentEditor: MarkdownEditorInstance) => {
      normalizeTopLevelImages(currentEditor);
      ensureTrailingParagraph(currentEditor);
    },
    [],
  );
  useTargetResolutionDom(
    rootRef,
    editor,
    targetResolutions,
    Boolean(adapters.targetResolver),
  );
  usePrivateImageSources(rootRef, editor, vault, document, commit);
  const imageHosts = useImageHosts(rootRef, editor, readOnly);

  const uploadImages = React.useCallback(
    async (files: File[], replacementPosition?: number) => {
      if (
        !editor ||
        readOnly ||
        uploadInFlightRef.current ||
        files.length === 0
      )
        return;
      const controller = new AbortController();
      uploadControllerRef.current = controller;
      uploadInFlightRef.current = true;
      setUploadFailure(null);
      setUploadingImage(true);
      onUploadingChangeRef.current?.(true);
      const failures: Array<{
        file: File;
        message: string;
        retryable: boolean;
      }> = [];
      const cancelled: File[] = [];
      let inserted = 0;
      let failed = false;
      try {
        for (const [index, file] of files.entries()) {
          if (controller.signal.aborted) {
            cancelled.push(file, ...files.slice(index + 1));
            break;
          }
          try {
            setUploadingName(`Checking ${file.name}`);
            const validationMessage = validateEditorImage(file);
            if (validationMessage)
              throw Object.assign(new Error(validationMessage), {
                status: 415,
              });
            const prepared = await prepareEditorImage(file);
            setUploadingName(
              prepared.optimized
                ? `Uploading optimized ${file.name}`
                : file.name,
            );
            const asset = await adapters.upload.upload(prepared.file, {
              vault,
              document,
              commit,
              signal: controller.signal,
            });
            const target = typeof asset.target === "string" ? asset.target : "";
            const assetId = assetIdFromUrl(target);
            if (!assetId)
              throw new Error(
                "The image upload returned an invalid asset URL.",
              );
            unclaimedAssetIdsRef.current.add(assetId);
            if (asset.expiresAt)
              unclaimedAssetExpirationsRef.current.set(
                assetId,
                asset.expiresAt,
              );
            reportAssetExpirations();
            reportUnclaimedAssetIds();
            if (!mountedRef.current)
              throw new DOMException("Upload cancelled", "AbortError");
            const alt = file.name.replace(/\.[^.]+$/, "") || "Image";
            if (replacementPosition !== undefined && inserted === 0) {
              const oldNode = editor.state.doc.nodeAt(replacementPosition);
              const oldTarget =
                oldNode?.type.name === "image"
                  ? String(oldNode.attrs.target ?? "")
                  : undefined;
              invokeCommand(editor, "focus");
              invokeCommand(editor, "setNodeSelection", replacementPosition);
              invokeCommand(editor, "updateAttributes", "image", {
                target,
                alt,
              });
              discardIfUnclaimed(oldTarget);
            } else commands.insertImage(target, alt);
            inserted += 1;
          } catch (error) {
            failed = true;
            if (
              controller.signal.aborted ||
              (error instanceof DOMException && error.name === "AbortError")
            ) {
              cancelled.push(file, ...files.slice(index + 1));
              break;
            }
            const failure = classifyEditorImageUploadFailure(error, file);
            failures.push({
              file,
              message: failure.message,
              retryable: failure.retryable,
            });
          }
        }
        if (mountedRef.current && (failures.length || cancelled.length)) {
          const retryable = failures
            .filter((failure) => failure.retryable)
            .map((failure) => failure.file);
          const queued = deferredImageFilesRef.current.splice(0);
          setUploadFailure({
            files: [...retryable, ...queued],
            message: [
              ...failures.map((failure) => failure.message),
              cancelled.length
                ? `${cancelled.length} image${cancelled.length === 1 ? "" : "s"} cancelled.`
                : "",
              "Successful images remain in the draft.",
            ]
              .filter(Boolean)
              .join(" "),
            retryable: retryable.length > 0 || queued.length > 0,
            kind: "error",
            replacementPosition,
          });
        }
      } finally {
        uploadControllerRef.current = null;
        uploadInFlightRef.current = false;
        if (mountedRef.current) {
          setUploadingImage(false);
          setUploadingName("");
          onUploadingChangeRef.current?.(false);
          if (!failed && deferredImageFilesRef.current.length > 0)
            setUploadFailure({
              files: deferredImageFilesRef.current.splice(0),
              message:
                "The previous image batch finished. Upload the next batch when ready.",
              retryable: true,
              kind: "queued",
              replacementPosition,
            });
        }
      }
    },
    [
      adapters.upload,
      commands,
      commit,
      discardIfUnclaimed,
      document,
      editor,
      readOnly,
      reportAssetExpirations,
      reportUnclaimedAssetIds,
      vault,
    ],
  );

  const handleImageDrop = (event: React.DragEvent<HTMLDivElement>) => {
    const files = transferredImages(event.dataTransfer);
    if (!files.length) return;
    event.preventDefault();
    event.stopPropagation();
    if (readOnly) return;
    if (uploadInFlightRef.current) deferredImageFilesRef.current.push(...files);
    else void uploadImages(files);
  };
  const handleImageDragOver = (event: React.DragEvent<HTMLDivElement>) => {
    const hasImage =
      transferredImages(event.dataTransfer).length > 0 ||
      Array.from(event.dataTransfer.items ?? []).some(
        (item) => item.kind === "file" && item.type.startsWith("image/"),
      );
    if (!hasImage) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = readOnly ? "none" : "copy";
  };

  const editorClassName = cn(
    "akb-markdown-content prose dark:prose-invert !max-w-none !min-h-96 w-full cursor-text outline-none font-sans text-[15px] leading-7 text-foreground",
    appearance === "canvas"
      ? "border-0 bg-transparent px-5 py-6 focus:outline-none focus-visible:ring-0 focus-visible:ring-offset-0 focus-within:ring-0 focus-within:ring-offset-0 sm:px-8 lg:px-10"
      : appearance === "workspace"
        ? "border-0 bg-transparent px-4 py-4 focus:outline-none focus-visible:ring-0 focus-visible:ring-offset-0 focus-within:ring-0 focus-within:ring-offset-0"
        : "border border-border bg-surface px-5 py-4 hover:border-foreground-muted focus-within:border-primary focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2 focus-within:ring-offset-background transition-colors",
    className,
  );
  const sourceClassName = cn(
    "min-h-96 w-full resize-y font-mono text-sm leading-6 text-foreground placeholder:text-foreground-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
    appearance === "canvas"
      ? "border-0 bg-transparent px-5 py-6 sm:px-8 lg:px-10"
      : appearance === "workspace"
        ? "border-0 bg-transparent px-4 py-4"
        : "border border-border bg-surface px-5 py-4 transition-colors",
  );
  React.useLayoutEffect(() => {
    const content = editorContentElement(rootRef.current);
    if (!content) return;
    content.className = editorClassName;
    content.setAttribute("role", "textbox");
    content.setAttribute("aria-multiline", "true");
    if (ariaLabel) content.setAttribute("aria-label", ariaLabel);
    else content.removeAttribute("aria-label");
    if (ariaLabelledby) content.setAttribute("aria-labelledby", ariaLabelledby);
    else content.removeAttribute("aria-labelledby");
    if (required) content.setAttribute("aria-required", "true");
    else content.removeAttribute("aria-required");
    content.dataset.placeholder = placeholder;
  }, [
    ariaLabel,
    ariaLabelledby,
    editor,
    editorClassName,
    placeholder,
    required,
  ]);

  return (
    <div
      ref={rootRef}
      data-testid="markdown-editor"
      className="relative min-w-0"
    >
      <MarkdownEditingSurface
        editor={editor}
        markdown={value}
        onSourceChange={handleSourceChange}
        onMarkdownApplied={handleMarkdownApplied}
        readOnly={readOnly}
        table={AKB_MARKDOWN_TABLE_OPTIONS}
        modeSwitchDisabled={uploadingImage}
        onWysiwygDragOverCapture={handleImageDragOver}
        onWysiwygDropCapture={handleImageDrop}
        toolbar={
          !readOnly && editor ? (
            <EditorToolbar
              editor={editor}
              searchAdapter={adapters.search}
              vault={vault}
              uploadingImage={uploadingImage}
              onChooseImages={(files) => {
                const replacementPosition =
                  replacementPositionRef.current ?? undefined;
                replacementPositionRef.current = null;
                void uploadImages(files, replacementPosition);
              }}
              onOpenImagePicker={() => {
                replacementPositionRef.current = null;
                imageInputRef.current?.click();
              }}
              appearance={appearance}
              imageInputRef={imageInputRef}
            />
          ) : null
        }
        sourceAriaLabel={ariaLabel}
        sourceAriaLabelledby={ariaLabelledby}
        sourceRequired={required}
        sourceClassName={sourceClassName}
      >
        {uploadingImage && (
          <Alert
            variant="info"
            title="Uploading image"
            className="border-x border-t-0"
          >
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
            title={
              uploadFailure.kind === "queued"
                ? "Images waiting to upload"
                : "Image upload failed"
            }
            className="border-x border-t-0"
          >
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span className="min-w-0 flex-1">
                {uploadFailure.message}
                {uploadFailure.files.length > 0
                  ? ` ${uploadFailure.files.length} image${uploadFailure.files.length === 1 ? "" : "s"} remain in this batch.`
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
                        uploadFailure.replacementPosition,
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
                    replacementPositionRef.current =
                      uploadFailure.replacementPosition ?? null;
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
        <div
          className="relative min-w-0"
          onPasteCapture={(event) => {
            if (readOnly) return;
            const files = transferredImages(event.clipboardData);
            if (!files.length) return;
            if (!isStandaloneImageClipboard(event.clipboardData)) return;
            event.preventDefault();
            event.stopPropagation();
            if (uploadInFlightRef.current)
              deferredImageFilesRef.current.push(...files);
            else void uploadImages(files);
          }}
        >
          {editor ? (
            <EditorContent editor={editor} />
          ) : (
            <div
              role="status"
              aria-live="polite"
              className="min-h-96 bg-surface-2 p-5 text-sm text-foreground-muted"
            >
              Loading editor…
            </div>
          )}
          {editor &&
            imageHosts.map((host, index) =>
              createPortal(
                <ImageControls
                  key={`${host.target}-${index}`}
                  editor={editor}
                  image={host.image}
                  target={host.target}
                  alt={host.alt}
                  onReplace={(position) => {
                    replacementPositionRef.current = position;
                    imageInputRef.current?.click();
                  }}
                />,
                host.host,
              ),
            )}
        </div>
      </MarkdownEditingSurface>
    </div>
  );
}

export default MarkdownEditor;
