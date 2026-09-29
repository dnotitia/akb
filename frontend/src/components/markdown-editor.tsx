import * as React from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown } from "lucide-react";
import {
  MarkdownEditingSurface,
  MarkdownLocaleProvider,
  MarkdownSurface,
  MarkdownToolbar,
  useMarkdownEditor,
  useMarkdownReferenceResolutions,
  useMarkdownTargetResolutions,
  type MarkdownImageMenuOptions,
  type MarkdownLinkSearchLabels,
} from "@akb/markdown-editor/react";
import {
  extractMarkdownTargets,
} from "@akb/markdown-editor";
import type { MarkdownAsset } from "@akb/markdown-editor";
import { discardAsset } from "@/lib/api";
import { normalizeEditorLinkUrl } from "@/lib/editor-link";
import {
  canonicalAkbMarkdownTarget,
  createAkbMarkdownAdapters,
} from "@/lib/markdown-adapters";
import {
  assetIdFromUrl,
  EDITOR_IMAGE_MIME_TYPES,
} from "@/lib/image-assets";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

type MarkdownEditorInstance = NonNullable<ReturnType<typeof useMarkdownEditor>>;
type EditorMode = "wysiwyg" | "source";

function EditorModeMenu({ mode, disabled, onModeChange }: {
  mode: EditorMode;
  disabled: boolean;
  onModeChange: (mode: EditorMode) => void;
}) {
  const changed = React.useRef(false);
  const label = mode === "source" ? "Markdown" : "Visual";
  return (
    <DropdownMenu.Root modal={false} onOpenChange={(open) => { if (open) changed.current = false; }}>
      <DropdownMenu.Trigger asChild>
        <Button variant="ghost" size="sm" disabled={disabled} aria-label={`Editor mode: ${label}`} className="h-8 shrink-0 gap-1.5 px-2 text-xs text-foreground-muted">
          {label}<ChevronDown className="h-3.5 w-3.5" aria-hidden />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content align="end" sideOffset={6}
          onCloseAutoFocus={(event) => { if (changed.current) event.preventDefault(); }}
          className="z-[var(--z-popover)] min-w-40 rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md">
          <DropdownMenu.RadioGroup value={mode} onValueChange={(next) => {
            if (next !== "wysiwyg" && next !== "source") return;
            changed.current = next !== mode;
            onModeChange(next);
          }}>
            {([{ value: "wysiwyg", label: "Visual" }, { value: "source", label: "Markdown" }] as const).map((option) => (
              <DropdownMenu.RadioItem key={option.value} value={option.value}
                className="relative flex min-h-9 cursor-pointer items-center rounded-[var(--radius-sm)] py-1.5 pl-8 pr-3 text-sm outline-none data-[highlighted]:bg-surface-hover data-[state=checked]:text-link">
                <DropdownMenu.ItemIndicator className="absolute left-2"><Check className="h-4 w-4" aria-hidden /></DropdownMenu.ItemIndicator>
                {option.label}
              </DropdownMenu.RadioItem>
            ))}
          </DropdownMenu.RadioGroup>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

const AKB_MARKDOWN_IMAGE_MENU_OPTIONS: Omit<MarkdownImageMenuOptions, "onReplace"> = {
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

function imageAssetIdsFromMarkdown(markdown: string): string[] {
  const ids = new Set<string>();
  for (const target of extractMarkdownTargets(markdown)) {
    if (target.kind !== "attachment") continue;
    const id = assetIdFromUrl(target.target);
    if (id) ids.add(id);
  }
  return [...ids];
}

interface EditorToolbarProps {
  editor: MarkdownEditorInstance | null;
  searchAdapter?: ReturnType<typeof createAkbMarkdownAdapters>["search"];
  vault: string;
  appearance: "framed" | "canvas" | "workspace";
}

const AKB_MARKDOWN_TABLE_OPTIONS = {
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
      className={cn(
        "static! min-w-0 flex-1 border-b-0!",
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
};

export interface MarkdownEditorProps {
  value: string;
  onChange?: (markdown: string, assetIds: readonly string[]) => void;
  placeholder?: string;
  autoFocus?: boolean;
  readOnly?: boolean;
  className?: string;
  sourceClassName?: string;
  appearance?: "framed" | "canvas" | "workspace";
  ariaLabel?: string;
  ariaLabelledby?: string;
  required?: boolean;
  vault: string;
  onSlashOpenChange?: (open: boolean, dismiss?: () => void) => void;
  onReferenceOpenChange?: (open: boolean, dismiss?: () => void) => void;
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

function MarkdownEditorContent({
  value,
  onChange,
  placeholder = "Write in markdown — slash commands and shortcuts work.",
  autoFocus,
  readOnly = false,
  className,
  sourceClassName: sourceClassNameOverride,
  appearance = "framed",
  ariaLabel,
  ariaLabelledby,
  required,
  vault,
  onSlashOpenChange,
  onReferenceOpenChange,
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
  const [resolutionMarkdown, setResolutionMarkdown] = React.useState(value);
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
    resolutionMarkdown,
    adapters.targetResolver,
    { vault, document, commit },
  );
  const referenceResolutions = useMarkdownReferenceResolutions(
    resolutionMarkdown,
    adapters.reference,
    { vault, document, commit },
  );
  React.useEffect(() => {
    setResolutionMarkdown(value);
  }, [value]);
  const handleChange = React.useCallback(
    (next: string) => {
      setResolutionMarkdown(next);
      onChange?.(next, imageAssetIdsFromMarkdown(next));
    },
    [onChange],
  );
  const handleSourceChange = React.useCallback(
    (markdown: string) => {
      setResolutionMarkdown(markdown);
      onChange?.(markdown, imageAssetIdsFromMarkdown(markdown));
    },
    [onChange],
  );
  const editor = useMarkdownEditor({
    initialMarkdown: value,
    profile: "preserve",
    editable: !readOnly,
    onChange: handleChange,
    slash: React.useMemo(() => ({ onOpenChange: onSlashOpenChange }), [onSlashOpenChange]),
    reference: React.useMemo(
      () => ({
        adapter: adapters.reference,
        context: { vault, document, commit },
        onOpenChange: onReferenceOpenChange ?? onSlashOpenChange,
      }),
      [adapters.reference, commit, document, onReferenceOpenChange, onSlashOpenChange, vault],
    ),
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
    sourceClassNameOverride,
  );
  const contentAttributes = React.useMemo(() => ({
    role: "textbox",
    "aria-multiline": "true",
    "aria-label": ariaLabel,
    "aria-labelledby": ariaLabelledby,
    "aria-required": required ? "true" : undefined,
    "data-placeholder": placeholder,
  }), [
    ariaLabel,
    ariaLabelledby,
    placeholder,
    required,
  ]);

  return (
    <div
      data-testid="markdown-editor"
      className="relative min-w-0"
    >
      <MarkdownEditingSurface
        editor={editor}
        markdown={value}
        autoFocus={autoFocus}
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
        renderHeader={({ mode, onModeChange, disabled, toolbar }) => (
          <div className="flex min-h-11 items-start justify-between gap-1 border-b border-border bg-surface pr-2" data-editor-controls>
            {toolbar ?? <span className="px-4 py-3 text-xs text-foreground-muted">{mode === "source" ? "Markdown source" : "Read only"}</span>}
            <div className="shrink-0 py-1.5">
              <EditorModeMenu mode={mode} onModeChange={onModeChange} disabled={disabled} />
            </div>
          </div>
        )}
      >
        <MarkdownSurface
          editor={editor}
          editable={!readOnly}
          contentClassName={editorClassName}
          contentAttributes={contentAttributes}
          resolutions={targetResolutions}
          resolvingTargets={Boolean(adapters.targetResolver)}
          referenceResolutions={referenceResolutions}
          resolvingReferences={Boolean(adapters.reference?.resolve)}
        >
          <div
            role="status"
            aria-live="polite"
            className="min-h-96 bg-surface-2 p-5 text-sm text-foreground-muted"
          >
            Loading editor…
          </div>
        </MarkdownSurface>
      </MarkdownEditingSurface>
    </div>
  );
}

export function MarkdownEditor(props: MarkdownEditorProps) {
  return (
    <MarkdownLocaleProvider locale="en">
      <MarkdownEditorContent {...props} />
    </MarkdownLocaleProvider>
  );
}

export default MarkdownEditor;
