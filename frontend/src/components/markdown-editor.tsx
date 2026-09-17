import * as React from "react";
import {
  EditorContent,
  MarkdownEditingSurface,
  MarkdownToolbar,
  useMarkdownEditor,
  useMarkdownTargetResolutions,
  type MarkdownImageMenuOptions,
  type MarkdownLinkSearchLabels,
} from "@akb/markdown-editor/react";
import {
  extractMarkdownTargets,
  serializeEditorMarkdown,
} from "@akb/markdown-editor";
import type { MarkdownAsset } from "@akb/markdown-editor";
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
  EDITOR_IMAGE_MIME_TYPES,
} from "@/lib/image-assets";
import { cn } from "@/lib/utils";

type MarkdownEditorInstance = NonNullable<ReturnType<typeof useMarkdownEditor>>;

const AKB_MARKDOWN_IMAGE_MENU_OPTIONS: Omit<MarkdownImageMenuOptions, "onReplace"> = {
  labels: {
    editDescription: (alt) => alt ? `Edit image description: ${alt}` : "Edit image description",
    replaceImage: (alt) => alt ? `Replace image: ${alt}` : "Replace image",
    removeImage: (alt) => alt ? `Remove image: ${alt}` : "Remove image",
    dialogTitle: "Image description",
    dialogDescription: "This text is used as the image alt text and visible caption.",
    description: "Description",
    cancel: "Cancel",
    saveDescription: "Save description",
    descriptionRequired: "Describe the image so it remains understandable without sight.",
    closeDialog: "Close dialog",
  },
  classNames: {
    host: "flex items-center gap-1 rounded-[var(--radius-md)] border border-border bg-surface/90 p-1 shadow-sm backdrop-blur-sm",
    action: "h-7 w-7 text-foreground-muted hover:bg-surface-hover hover:text-foreground",
    destructiveAction: "h-7 w-7 text-foreground-muted hover:bg-destructive/10 hover:text-destructive",
    dialog: "sm:max-w-md",
    field: "rounded-[var(--radius-md)] focus-visible:ring-offset-background aria-[invalid=true]:border-destructive aria-[invalid=true]:focus-visible:ring-destructive",
    error: "text-xs text-destructive",
  },
  isEditableTarget: (target) => canonicalAkbMarkdownTarget(target) !== null,
};

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

interface EditorToolbarProps {
  editor: MarkdownEditorInstance | null;
  searchAdapter?: ReturnType<typeof createAkbMarkdownAdapters>["search"];
  vault: string;
  appearance: "framed" | "canvas" | "workspace";
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
  appearance,
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
    />
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
  const unclaimedAssetIdsRef = React.useRef(new Set(initialUnclaimedAssetIds));
  const unclaimedAssetExpirationsRef = React.useRef(
    new Map(Object.entries(initialUnclaimedAssetExpirations)),
  );
  const discardingAssetIdsRef = React.useRef(new Set<string>());
  const preserveUploadsOnUnmountRef = React.useRef(preserveUploadsOnUnmount);
  const onUploadingChangeRef = React.useRef(onUploadingChange);
  const onAssetExpirationsChangeRef = React.useRef(onAssetExpirationsChange);
  const onUnclaimedAssetIdsChangeRef = React.useRef(onUnclaimedAssetIdsChange);
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

  const handleAssetUploaded = React.useCallback((asset: MarkdownAsset) => {
    const assetId = asset.id ?? assetIdFromUrl(asset.target);
    if (!assetId) return;
    unclaimedAssetIdsRef.current.add(assetId);
    if (asset.expiresAt) {
      unclaimedAssetExpirationsRef.current.set(assetId, asset.expiresAt);
    }
    reportAssetExpirations();
    reportUnclaimedAssetIds();
  }, [reportAssetExpirations, reportUnclaimedAssetIds]);

  const imageUpload = React.useMemo(
    () => ({
      adapter: adapters.upload,
      context: { vault, document, commit },
      accept: EDITOR_IMAGE_MIME_TYPES.join(","),
      classNames: { status: "border-x border-t-0" },
      onUploadingChange: (uploading: boolean) => {
        onUploadingChangeRef.current?.(uploading);
      },
      onAssetUploaded: handleAssetUploaded,
      onAssetReplaced: (previousTarget: string) => {
        discardIfUnclaimed(previousTarget);
      },
    }),
    [adapters.upload, commit, discardIfUnclaimed, document, handleAssetUploaded, vault],
  );

  React.useEffect(() => {
    reportUnclaimedAssetIds();
  }, [reportUnclaimedAssetIds]);
  React.useEffect(() => {
    const unclaimedAssetIds = unclaimedAssetIdsRef.current;
    return () => {
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
    if (autoFocus && !readOnly)
      requestAnimationFrame(() => editor.commands.focus());
  }, [autoFocus, editor, readOnly]);
  useTargetResolutionDom(
    rootRef,
    editor,
    targetResolutions,
    Boolean(adapters.targetResolver),
  );
  usePrivateImageSources(rootRef, editor, vault, document, commit);

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
        readOnly={readOnly}
        table={AKB_MARKDOWN_TABLE_OPTIONS}
        imageMenu={AKB_MARKDOWN_IMAGE_MENU_OPTIONS}
        imageUpload={imageUpload}
        toolbar={
          !readOnly && editor ? (
            <EditorToolbar
              editor={editor}
              searchAdapter={adapters.search}
              vault={vault}
              appearance={appearance}
            />
          ) : null
        }
        sourceAriaLabel={ariaLabel}
        sourceAriaLabelledby={ariaLabelledby}
        sourceRequired={required}
        sourceClassName={sourceClassName}
      >
        <div className="relative min-w-0">
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
        </div>
      </MarkdownEditingSurface>
    </div>
  );
}

export default MarkdownEditor;
