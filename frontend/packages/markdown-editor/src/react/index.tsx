import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ComponentPropsWithoutRef, DragEventHandler, ReactNode } from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { EditorContent, useEditor } from '@tiptap/react'
import type { Editor } from '@tiptap/core'
import {
  Bold,
  Code,
  Code2,
  Heading1,
  Heading2,
  Heading3,
  ImagePlus,
  Italic,
  Link2,
  List,
  ListOrdered,
  Loader2,
  Minus,
  Pilcrow,
  Quote,
  Redo2,
  Strikethrough,
  Table,
  Undo2,
  X,
} from 'lucide-react'

import { createMarkdownExtensions } from '../extensions.js'
import { extractMarkdownTargets, markdownCommands, serializeEditorMarkdown } from '../core.js'
import { normalizeMarkdownLinkUrl } from '../link.js'
import { resolveMarkdownTargets } from '../adapters.js'
import { MarkdownLinkSearch } from './markdown-link-search.js'
import type { MarkdownLinkSearchLabels } from './markdown-link-search.js'
export type { MarkdownLinkSearchLabels } from './markdown-link-search.js'
import { MarkdownImageMenuControls } from './markdown-image-menu.js'
import type { MarkdownImageMenuOptions } from './markdown-image-menu.js'
export type { MarkdownImageMenuClassNames, MarkdownImageMenuLabels, MarkdownImageMenuOptions } from './markdown-image-menu.js'
import { MarkdownTableControls, DEFAULT_MARKDOWN_TABLE_LABELS } from './markdown-table.js'
import type { MarkdownTableLabels, MarkdownTableOptions } from './markdown-table.js'
export type { MarkdownTableLabels, MarkdownTableOptions } from './markdown-table.js'
import {
  MarkdownImageUploadProvider,
  MarkdownImageUploadStatus,
  MarkdownImageUploadInput,
  useMarkdownImageUpload,
  useMarkdownImageUploadContext,
} from './markdown-image-upload.js'
export type {
  MarkdownImageUploadClassNames,
  MarkdownImageUploadController,
  MarkdownImageUploadFailure,
  MarkdownImageUploadLabels,
  MarkdownImageUploadOptions,
  MarkdownImageUploadState,
} from './markdown-image-upload.js'
export {
  DEFAULT_MARKDOWN_IMAGE_UPLOAD_LABELS,
  MarkdownImageUploadProvider,
  MarkdownImageUploadStatus,
  MarkdownImageUploadInput,
  useMarkdownImageUpload,
  useMarkdownImageUploadContext,
} from './markdown-image-upload.js'
import { markdownTableState } from '../table.js'
import type {
  MarkdownAdapters,
  MarkdownCommands,
  MarkdownEditorConfig,
  MarkdownHeadingLevel,
  MarkdownLinkLabels,
  MarkdownLinkUrlNormalizer,
  MarkdownProfile,
  MarkdownSearchAdapter,
  MarkdownSearchContext,
  MarkdownSlashContext,
  MarkdownState,
  MarkdownTargetResolution,
  MarkdownTargetResolverContext,
} from '../types.js'
import type { MarkdownImageUploadOptions } from './markdown-image-upload.js'

const EMPTY_RESOLUTIONS: ReadonlyMap<string, MarkdownTargetResolution> = new Map()

export interface UseMarkdownEditorOptions {
  initialMarkdown?: string
  profile?: MarkdownProfile
  editable?: boolean
  onChange?: MarkdownEditorConfig['onChange']
  onSlash?: (context: MarkdownSlashContext) => void
}

export function useMarkdownEditor({
  initialMarkdown = '',
  profile = 'preserve',
  editable = true,
  onChange,
  onSlash,
}: UseMarkdownEditorOptions = {}): Editor | null {
  const extensions = useMemo(
    () => createMarkdownExtensions({ profile, onSlash }),
    [onSlash, profile],
  )

  return useEditor({
    extensions,
    content: initialMarkdown,
    contentType: 'markdown',
    editable,
    immediatelyRender: false,
    onUpdate: ({ editor }) => onChange?.(editor.getMarkdown(), editor),
  })
}

export function useMarkdownCommands(editor: Editor | null): MarkdownCommands {
  return useMemo(
    () =>
      editor
        ? markdownCommands(editor)
        : {
            setMarkdown: () => false,
            insertMarkdown: () => false,
            insertImage: () => false,
            replaceImageAt: () => false,
            setImageAltAt: () => false,
            deleteImageAt: () => false,
            setLink: () => false,
            insertLink: () => false,
            unsetLink: () => false,
            insertTable: () => false,
            addTableRowAfter: () => false,
            addTableColumnAfter: () => false,
            deleteTableRow: () => false,
            deleteTableColumn: () => false,
            deleteTable: () => false,
            continueBelowTable: () => false,
            setParagraph: () => false,
            toggleHeading: () => false,
            toggleBold: () => false,
            toggleItalic: () => false,
            toggleStrike: () => false,
            toggleCode: () => false,
            toggleBulletList: () => false,
            toggleOrderedList: () => false,
            toggleBlockquote: () => false,
            toggleCodeBlock: () => false,
            setHorizontalRule: () => false,
            undo: () => false,
            redo: () => false,
            focus: () => false,
          },
    [editor],
  )
}

function readState(editor: Editor): MarkdownState {
  return {
    markdown: editor.getMarkdown(),
    isEmpty: editor.isEmpty,
    isEditable: editor.isEditable,
    table: markdownTableState(editor),
    active: {
      paragraph: editor.isActive('paragraph'),
      heading1: editor.isActive('heading', { level: 1 }),
      heading2: editor.isActive('heading', { level: 2 }),
      heading3: editor.isActive('heading', { level: 3 }),
      bold: editor.isActive('bold'),
      italic: editor.isActive('italic'),
      strike: editor.isActive('strike'),
      code: editor.isActive('code'),
      bulletList: editor.isActive('bulletList'),
      orderedList: editor.isActive('orderedList'),
      blockquote: editor.isActive('blockquote'),
      codeBlock: editor.isActive('codeBlock'),
      link: editor.isActive('link'),
    },
    link: {
      active: editor.isActive('link'),
      href: String(editor.getAttributes('link').href ?? ''),
    },
    canUndo: editor.can().undo(),
    canRedo: editor.can().redo(),
    selection: {
      from: editor.state.selection.from,
      to: editor.state.selection.to,
    },
  }
}

export function useMarkdownState(editor: Editor | null): MarkdownState | null {
  const [state, setState] = useState<MarkdownState | null>(() => (editor ? readState(editor) : null))

  useEffect(() => {
    if (!editor) {
      return
    }

    const update = () => setState(readState(editor))
    update()
    editor.on('transaction', update)
    editor.on('selectionUpdate', update)

    return () => {
      editor.off('transaction', update)
      editor.off('selectionUpdate', update)
    }
  }, [editor])

  return editor ? state : null
}

/**
 * Resolve canonical targets for presentation. The returned map is ephemeral;
 * it is never fed back into the editor document or its Markdown serializer.
 */
export function useMarkdownTargetResolutions(
  markdown: string,
  resolver?: MarkdownAdapters['targetResolver'],
  context: MarkdownTargetResolverContext = {},
): ReadonlyMap<string, MarkdownTargetResolution> {
  const { commit, document, vault } = context
  const targets = useMemo(() => extractMarkdownTargets(markdown), [markdown])
  const resolutionKey = useMemo(
    () => [vault ?? '', document ?? '', commit ?? '', ...targets.map(target => target.target)].join('\u0000'),
    [commit, document, targets, vault],
  )
  const [resolutionState, setResolutionState] = useState<{
    key: string
    resolutions: ReadonlyMap<string, MarkdownTargetResolution>
  }>({ key: '', resolutions: EMPTY_RESOLUTIONS })

  useEffect(() => {
    if (!resolver || targets.length === 0) return

    const controller = new AbortController()
    void resolveMarkdownTargets(resolver, targets, {
      vault,
      document,
      commit,
      signal: controller.signal,
    }).then(next => {
      if (!controller.signal.aborted) setResolutionState({ key: resolutionKey, resolutions: next })
    })

    return () => controller.abort()
  }, [commit, document, resolutionKey, resolver, targets, vault])

  return resolver && targets.length > 0 && resolutionState.key === resolutionKey
    ? resolutionState.resolutions
    : EMPTY_RESOLUTIONS
}

interface MarkdownSurfaceProps extends Omit<ComponentPropsWithoutRef<'div'>, 'onChange'> {
  editor: Editor | null
  editable: boolean
  resolutions?: ReadonlyMap<string, MarkdownTargetResolution>
  resolvingTargets?: boolean
  children?: ReactNode
}

function MarkdownSurface({
  editor,
  editable,
  resolutions = EMPTY_RESOLUTIONS,
  resolvingTargets = false,
  children,
  ...props
}: MarkdownSurfaceProps) {
  useEffect(() => {
    const root = editor?.view.dom
    if (!root) return

    const applyResolutions = () => {
      root.querySelectorAll<HTMLElement>('img[data-markdown-target], a[href]').forEach(element => {
        const target =
          element.dataset.markdownTarget ??
          (element.tagName === 'A' && element.getAttribute('href')?.startsWith('akb://')
            ? element.getAttribute('href')
            : null)
        if (!target) return
        element.dataset.markdownTarget = target
        const resolution = resolutions.get(target)
        const previousResolution = element.dataset.markdownResolution
        if (!resolution) {
          if (resolvingTargets) {
            element.dataset.markdownResolution = 'pending'
            element.setAttribute('aria-disabled', 'true')
            if (element.tagName === 'IMG') element.removeAttribute('src')
            else element.setAttribute('href', '#')
            return
          }
          if (element.tagName === 'IMG') element.setAttribute('src', target)
          else element.setAttribute('href', target)
          if (previousResolution === 'unavailable') element.removeAttribute('aria-label')
          element.removeAttribute('aria-disabled')
          delete element.dataset.markdownResolution
          return
        }

        if (resolution.status === 'available' && resolution.runtimeUrl) {
          if (element.tagName === 'IMG') element.setAttribute('src', resolution.runtimeUrl)
          else element.setAttribute('href', resolution.runtimeUrl)
          element.dataset.markdownResolution = 'available'
          if (previousResolution === 'unavailable') element.removeAttribute('aria-label')
          element.removeAttribute('aria-disabled')
          return
        }

        element.dataset.markdownResolution = 'unavailable'
        element.setAttribute('aria-label', resolution.label ?? 'Reference unavailable')
        if (element.tagName === 'IMG') element.removeAttribute('src')
        else {
          element.setAttribute('href', '#')
          element.setAttribute('aria-disabled', 'true')
        }
      })
    }

    applyResolutions()
    editor.on('transaction', applyResolutions)
    return () => {
      editor.off('transaction', applyResolutions)
    }
  }, [editor, resolvingTargets, resolutions])

  return (
    <div
      {...props}
      data-markdown-surface={editable ? 'editor' : 'viewer'}
      data-markdown-ready={editor ? 'true' : 'false'}
    >
      {editor ? <EditorContent editor={editor} /> : children}
    </div>
  )
}

export type MarkdownEditorMode = 'wysiwyg' | 'source'

export interface MarkdownEditingSurfaceLabels {
  group: string
  wysiwyg: string
  source: string
  sourceField: string
}

function normalizeEditorBody(editor: Editor): void {
  const document = editor.getJSON()
  if (document.content?.some(node => node.type === 'image')) {
    const content = document.content.map(node =>
      node.type === 'image' ? { type: 'paragraph', content: [node] } : node,
    )
    editor.commands.setContent({ ...document, content }, { emitUpdate: false })
  }

  const last = editor.state.doc.lastChild
  if (!last || last.type.name !== 'paragraph') {
    editor.commands.insertContentAt(editor.state.doc.content.size, { type: 'paragraph' })
  }
}

const DEFAULT_EDITING_SURFACE_LABELS: MarkdownEditingSurfaceLabels = {
  group: 'Editor mode',
  wysiwyg: 'WYSIWYG',
  source: 'Source',
  sourceField: 'Markdown source',
}

export interface MarkdownEditingSurfaceProps extends Omit<ComponentPropsWithoutRef<'div'>, 'onChange'> {
  editor: Editor | null
  markdown: string
  profile?: MarkdownProfile
  onSourceChange?: (markdown: string, editor: Editor) => void
  onMarkdownApplied?: (editor: Editor) => void
  readOnly?: boolean
  modeSwitchDisabled?: boolean
  toolbar?: ReactNode
  table?: MarkdownTableOptions
  imageMenu?: MarkdownImageMenuOptions
  imageUpload?: MarkdownImageUploadOptions
  sourcePlaceholder?: string
  sourceLabel?: string
  sourceAriaLabel?: string
  sourceAriaLabelledby?: string
  sourceRequired?: boolean
  sourceClassName?: string
  modeLabels?: Partial<MarkdownEditingSurfaceLabels>
  onWysiwygDragOverCapture?: DragEventHandler<HTMLDivElement>
  onWysiwygDropCapture?: DragEventHandler<HTMLDivElement>
  children?: ReactNode
}

/**
 * Keep one editor mounted while the same draft moves between visual and
 * Markdown editing. The consumer owns persistence and adapters; this surface
 * owns mode switching, source synchronization, focus, and the shared source UI.
 */
export function MarkdownEditingSurface({
  editor,
  markdown,
  profile = 'preserve',
  onSourceChange,
  onMarkdownApplied,
  readOnly = false,
  modeSwitchDisabled = false,
  toolbar,
  table,
  imageMenu,
  imageUpload,
  sourcePlaceholder = 'Write Markdown source…',
  sourceLabel,
  sourceAriaLabel,
  sourceAriaLabelledby,
  sourceRequired = false,
  sourceClassName,
  modeLabels,
  onWysiwygDragOverCapture,
  onWysiwygDropCapture,
  children,
  className,
  ...props
}: MarkdownEditingSurfaceProps) {
  const imageUploadController = useMarkdownImageUpload(editor, imageUpload, readOnly)
  const labels = { ...DEFAULT_EDITING_SURFACE_LABELS, ...modeLabels }
  const [mode, setMode] = useState<MarkdownEditorMode>('wysiwyg')
  const [source, setSource] = useState(markdown)
  const sourceRef = useRef(markdown)
  const sourceDirtyRef = useRef(false)
  const lastMarkdownRef = useRef(markdown)
  const previousModeRef = useRef(mode)
  const sourceInputRef = useRef<HTMLTextAreaElement>(null)
  const sourceInputId = useId()
  const sourceInputLabelId = `${sourceInputId}-label`
  const surfaceRef = useRef<HTMLDivElement>(null)
  const resolvedSourceLabel = sourceLabel ?? labels.sourceField
  const effectiveImageMenu = imageUploadController && imageMenu
    ? {
        ...imageMenu,
        onReplace: (position: number) => imageUploadController.beginReplacement(position),
      }
    : imageMenu
  const effectiveModeSwitchDisabled = modeSwitchDisabled || Boolean(imageUploadController?.state.uploading)

  const handleWysiwygDragOverCapture: DragEventHandler<HTMLDivElement> = event => {
    imageUploadController?.handleDragOver(event)
    onWysiwygDragOverCapture?.(event)
  }
  const handleWysiwygDropCapture: DragEventHandler<HTMLDivElement> = event => {
    imageUploadController?.handleDrop(event)
    onWysiwygDropCapture?.(event)
  }

  const wysiwygPanel = (
    <div
      hidden={mode !== 'wysiwyg'}
      data-markdown-mode-panel="wysiwyg"
      onDragOverCapture={handleWysiwygDragOverCapture}
      onDropCapture={handleWysiwygDropCapture}
      onPasteCapture={event => imageUploadController?.handlePaste(event)}
    >
      {mode === 'wysiwyg' ? toolbar : null}
      {imageUploadController ? (
        <MarkdownImageUploadStatus options={imageUpload} />
      ) : null}
      {children}
      {imageUploadController ? (
        <MarkdownImageUploadInput
          accept={imageUpload?.accept}
          className={imageUpload?.classNames?.input}
        />
      ) : null}
    </div>
  )

  useLayoutEffect(() => {
    if (sourceDirtyRef.current) return
    sourceRef.current = markdown
    setSource(markdown)
  }, [markdown])

  useLayoutEffect(() => {
    const externalValueChanged = markdown !== lastMarkdownRef.current
    lastMarkdownRef.current = markdown
    if (!editor || (mode === 'source' && sourceDirtyRef.current)) return

    if (externalValueChanged && editor.getMarkdown() !== markdown) {
      editor.commands.setContent(markdown, {
        contentType: 'markdown',
        emitUpdate: false,
      })
      normalizeEditorBody(editor)
      onMarkdownApplied?.(editor)
    }
  }, [editor, markdown, mode, onMarkdownApplied])

  useLayoutEffect(() => {
    if (editor) normalizeEditorBody(editor)
  }, [editor])

  useEffect(() => {
    if (editor && editor.isEditable !== !readOnly) editor.setEditable(!readOnly, false)
  }, [editor, readOnly])

  useEffect(() => {
    if (previousModeRef.current === mode) return
    previousModeRef.current = mode

    if (mode === 'source') sourceInputRef.current?.focus()
    else editor?.commands.focus()
  }, [editor, mode])

  const selectMode = (nextMode: MarkdownEditorMode) => {
    if (!editor || nextMode === mode) return

    if (nextMode === 'source') {
      const editorMarkdown = serializeEditorMarkdown(editor, { profile })
      sourceRef.current = editorMarkdown
      sourceDirtyRef.current = false
      setSource(editorMarkdown)
    } else {
      const sourceMarkdown = sourceRef.current
      if (sourceDirtyRef.current && editor.getMarkdown() !== sourceMarkdown) {
        editor.commands.setContent(sourceMarkdown, {
          contentType: 'markdown',
          emitUpdate: false,
        })
        normalizeEditorBody(editor)
        onMarkdownApplied?.(editor)
      }
      sourceDirtyRef.current = false
    }

    setMode(nextMode)
  }

  const modeButton = (target: MarkdownEditorMode, label: string) => (
    <button
      type="button"
      aria-pressed={mode === target}
      disabled={!editor || effectiveModeSwitchDisabled}
      onClick={() => selectMode(target)}
      className={joinClasses(
        'inline-flex h-8 items-center justify-center rounded-[var(--radius-sm)] px-3 text-sm font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-50',
        mode === target
          ? 'bg-surface-selected text-surface-selected-foreground'
          : 'text-foreground-muted hover:bg-surface-hover hover:text-foreground',
      )}
    >
      {label}
    </button>
  )

  return (
    <div
      {...props}
      ref={surfaceRef}
      className={joinClasses('relative min-w-0', className)}
      data-markdown-mode={mode}
    >
      <div className="flex justify-end border-b border-border bg-surface px-2 py-1.5">
        <div
          role="group"
          aria-label={labels.group}
          className="inline-flex items-center gap-0.5 rounded-[var(--radius-md)] bg-surface-2 p-0.5"
          data-markdown-mode-toggle
        >
          {modeButton('wysiwyg', labels.wysiwyg)}
          {modeButton('source', labels.source)}
        </div>
      </div>

      {imageUploadController ? (
        <MarkdownImageUploadProvider controller={imageUploadController}>
          {wysiwygPanel}
        </MarkdownImageUploadProvider>
      ) : wysiwygPanel}

      <div hidden={mode !== 'source'} data-markdown-mode-panel="source">
        <label id={sourceInputLabelId} htmlFor={sourceInputId} className="sr-only">
          {resolvedSourceLabel}
        </label>
        <textarea
          ref={sourceInputRef}
          id={sourceInputId}
          aria-label={sourceAriaLabel}
          aria-labelledby={
            sourceAriaLabelledby
              ? `${sourceAriaLabelledby} ${sourceInputLabelId}`
              : undefined
          }
          aria-required={sourceRequired || undefined}
          readOnly={readOnly}
          spellCheck={false}
          placeholder={sourcePlaceholder}
          value={source}
          onChange={event => {
            const next = event.currentTarget.value
            sourceRef.current = next
            sourceDirtyRef.current = true
            setSource(next)
            if (editor && !readOnly) onSourceChange?.(next, editor)
          }}
          className={
            sourceClassName ??
            'min-h-96 w-full resize-y border-0 bg-surface px-4 py-4 font-mono text-sm leading-6 text-foreground placeholder:text-foreground-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring focus-visible:ring-offset-0'
          }
        />
      </div>
      <MarkdownTableControls editor={editor} readOnly={readOnly} options={table} />
      <MarkdownImageMenuControls
        editor={editor}
        rootRef={surfaceRef}
        readOnly={readOnly}
        options={effectiveImageMenu}
      />
    </div>
  )
}

export interface MarkdownEditorProps extends Omit<MarkdownSurfaceProps, 'editor' | 'editable'> {
  markdown: string
  profile?: MarkdownProfile
  readOnly?: boolean
  onChange?: MarkdownEditorConfig['onChange']
  onSlash?: (context: MarkdownSlashContext) => void
  adapters?: MarkdownAdapters
  resolverContext?: MarkdownTargetResolverContext
  imageMenu?: MarkdownImageMenuOptions
  imageUpload?: MarkdownImageUploadOptions
}

export function MarkdownEditor({
  markdown,
  profile = 'preserve',
  readOnly = false,
  onChange,
  onSlash,
  adapters,
  resolverContext,
  imageMenu,
  imageUpload,
  ...props
}: MarkdownEditorProps) {
  const editor = useMarkdownEditor({
    initialMarkdown: markdown,
    profile,
    editable: !readOnly,
    onChange,
    onSlash,
  })
  const resolutions = useMarkdownTargetResolutions(
    markdown,
    adapters?.targetResolver,
    resolverContext,
  )

  return (
    <MarkdownEditingSurface
      {...props}
      editor={editor}
      markdown={markdown}
      profile={profile}
      readOnly={readOnly}
      imageMenu={imageMenu}
      imageUpload={imageUpload}
      onSourceChange={(next, sourceEditor) => onChange?.(next, sourceEditor)}
    >
      <MarkdownSurface
        editor={editor}
        editable={!readOnly}
        resolutions={resolutions}
        resolvingTargets={Boolean(adapters?.targetResolver)}
      />
    </MarkdownEditingSurface>
  )
}

export interface MarkdownViewerProps extends Omit<MarkdownSurfaceProps, 'editor' | 'editable'> {
  markdown: string
  profile?: MarkdownProfile
  adapters?: MarkdownAdapters
  resolverContext?: MarkdownTargetResolverContext
}

export function MarkdownViewer({
  markdown,
  profile = 'preserve',
  adapters,
  resolverContext,
  ...props
}: MarkdownViewerProps) {
  const editor = useMarkdownEditor({
    initialMarkdown: markdown,
    profile,
    editable: false,
  })
  const resolutions = useMarkdownTargetResolutions(
    markdown,
    adapters?.targetResolver,
    resolverContext,
  )

  useEffect(() => {
    if (!editor || editor.getMarkdown() === markdown) {
      return
    }

    editor.commands.setContent(markdown, { contentType: 'markdown' })
  }, [editor, markdown])

  return (
    <MarkdownSurface
      {...props}
      editor={editor}
      editable={false}
      resolutions={resolutions}
      resolvingTargets={Boolean(adapters?.targetResolver)}
    />
  )
}

export { EditorContent }

function joinClasses(...classes: Array<string | undefined>): string {
  return classes.filter(Boolean).join(' ')
}

const DEFAULT_MARKDOWN_LINK_LABELS: MarkdownLinkLabels = {
  insertButton: 'Insert link',
  editButton: 'Edit link',
  saveButton: 'Save link',
  insertTitle: 'Insert link',
  editTitle: 'Edit link',
  description: 'Add a safe destination and choose the text readers will see.',
  url: 'URL',
  text: 'Text',
  textPlaceholder: 'Link text',
  textHint: 'Leave blank to use the destination as the visible text.',
  cancel: 'Cancel',
  remove: 'Remove link',
  close: 'Close dialog',
  invalidUrl: 'Enter an http(s), email, phone, anchor, or relative URL.',
}

const linkInputClass =
  'flex h-10 w-full rounded-[var(--radius-md)] border border-border bg-surface px-3 py-2 text-sm text-foreground placeholder:text-foreground-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface aria-[invalid=true]:border-destructive aria-[invalid=true]:focus-visible:ring-destructive'

const linkButtonClass =
  'inline-flex h-9 items-center justify-center gap-2 rounded-[var(--radius-md)] border border-border px-4 text-sm font-medium text-foreground transition-token hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-50'

interface MarkdownLinkSelectionSnapshot {
  from: number
  to: number
  href: string
  active: boolean
  text: string
}

export interface MarkdownLinkPopupProps {
  editor: Editor | null
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Product-specific canonicalization and URL policy. */
  normalizeUrl?: MarkdownLinkUrlNormalizer
  /** Product-owned search and authorization context; the popup supplies signal. */
  searchAdapter?: MarkdownSearchAdapter
  searchContext?: Omit<MarkdownSearchContext, 'signal'>
  searchLabels?: Partial<MarkdownLinkSearchLabels>
  searchClassName?: string
  labels?: Partial<MarkdownLinkLabels>
  className?: string
}

/**
 * Shared link editor. It snapshots the editor selection before focus leaves the
 * document, and restores that snapshot for every cancel/error path. Applying,
 * editing, and removing a link are routed through the public command contract;
 * consumers never need to assemble Tiptap commands themselves.
 */
export function MarkdownLinkPopup({
  editor,
  open,
  onOpenChange,
  normalizeUrl = normalizeMarkdownLinkUrl,
  searchAdapter,
  searchContext,
  searchLabels,
  searchClassName,
  labels,
  className,
}: MarkdownLinkPopupProps) {
  const copy = { ...DEFAULT_MARKDOWN_LINK_LABELS, ...labels }
  const commands = useMarkdownCommands(editor)
  const urlInputRef = useRef<HTMLInputElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const snapshotRef = useRef<MarkdownLinkSelectionSnapshot | null>(null)
  const previousOpenRef = useRef(false)
  const closeReasonRef = useRef<'cancel' | 'commit'>('cancel')
  const [snapshot, setSnapshot] = useState<MarkdownLinkSelectionSnapshot | null>(null)
  const [linkUrl, setLinkUrl] = useState('')
  const [linkText, setLinkText] = useState('')
  const [linkError, setLinkError] = useState('')
  const linkUrlId = useId()
  const linkTextId = useId()

  const focusPopupInput = useCallback(() => {
    (searchAdapter ? searchInputRef.current : urlInputRef.current)?.focus()
  }, [searchAdapter])

  const restoreSelection = useCallback(() => {
    if (!editor || editor.isDestroyed || !snapshotRef.current) return
    const { from, to } = snapshotRef.current
    editor.commands.setTextSelection({ from, to })
    editor.commands.focus()
  }, [editor])

  useEffect(() => {
    if (open && !previousOpenRef.current && editor && !editor.isDestroyed) {
      const { from, to } = editor.state.selection
      const snapshot = {
        from,
        to,
        href: String(editor.getAttributes('link').href ?? ''),
        active: editor.isActive('link'),
        text: editor.state.doc.textBetween(from, to, ' '),
      } satisfies MarkdownLinkSelectionSnapshot
      snapshotRef.current = snapshot
      setSnapshot(snapshot)
      closeReasonRef.current = 'cancel'
      setLinkUrl(snapshot.href)
      setLinkText(snapshot.text)
      setLinkError('')
      requestAnimationFrame(focusPopupInput)
    }

    if (!open && previousOpenRef.current && closeReasonRef.current === 'cancel') {
      restoreSelection()
    }

    previousOpenRef.current = open
  }, [editor, focusPopupInput, open, restoreSelection])

  const closePopup = (reason: 'cancel' | 'commit') => {
    closeReasonRef.current = reason
    if (reason === 'cancel') restoreSelection()
    setSnapshot(null)
    onOpenChange(false)
    requestAnimationFrame(() => {
      if (!editor || editor.isDestroyed) return
      if (reason === 'cancel') restoreSelection()
      else editor.commands.focus()
    })
  }

  const applyLink = () => {
    const snapshot = snapshotRef.current
    if (!editor || editor.isDestroyed || !snapshot || !editor.isEditable) return

    const normalizedUrl = normalizeUrl(linkUrl)
    if (!normalizedUrl) {
      setLinkError(copy.invalidUrl)
      requestAnimationFrame(() => urlInputRef.current?.focus())
      return
    }

    restoreSelection()
    const applied = snapshot.active
      ? commands.setLink(normalizedUrl)
      : snapshot.from === snapshot.to
        ? commands.insertLink(linkText.trim() || normalizedUrl, normalizedUrl)
        : commands.setLink(normalizedUrl)
    if (!applied) return
    closePopup('commit')
  }

  const removeLink = () => {
    if (!editor || editor.isDestroyed || !snapshotRef.current || !editor.isEditable) return
    restoreSelection()
    if (!commands.unsetLink()) return
    closePopup('commit')
  }

  return (
    <DialogPrimitive.Root open={open} onOpenChange={next => (next ? onOpenChange(true) : closePopup('cancel'))}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-[var(--z-overlay)] bg-black/50" />
        <DialogPrimitive.Content
          className={joinClasses(
            'fixed left-1/2 top-1/2 z-[var(--z-modal)] grid max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 gap-4 overflow-y-auto rounded-[var(--radius-xl)] border border-border bg-surface p-6 text-foreground shadow-lg focus:outline-none',
            className,
          )}
          data-markdown-link-popup
          onOpenAutoFocus={event => {
            event.preventDefault()
            requestAnimationFrame(focusPopupInput)
          }}
          onCloseAutoFocus={event => {
            event.preventDefault()
            if (closeReasonRef.current === 'cancel') restoreSelection()
            else if (editor && !editor.isDestroyed) editor.commands.focus()
          }}
        >
          <div className="flex flex-col gap-1.5 text-left">
            <DialogPrimitive.Title className="text-lg font-semibold tracking-tight">
              {snapshot?.active ? copy.editTitle : copy.insertTitle}
            </DialogPrimitive.Title>
            <DialogPrimitive.Description className="text-sm text-foreground-muted">
              {copy.description}
            </DialogPrimitive.Description>
          </div>
          <DialogPrimitive.Close
            type="button"
            aria-label={copy.close}
            className="absolute right-2 top-2 inline-flex h-9 w-9 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
            onClick={event => {
              event.preventDefault()
              closePopup('cancel')
            }}
          >
            <X className="h-4 w-4" aria-hidden />
          </DialogPrimitive.Close>
          <div className="space-y-4">
            {searchAdapter && open && (
              <MarkdownLinkSearch
                adapter={searchAdapter}
                context={searchContext}
                labels={searchLabels}
                className={searchClassName}
                inputRef={searchInputRef}
                onSelect={(result) => {
                  setLinkUrl(result.target)
                  const selection = snapshotRef.current
                  if (selection && !selection.active && selection.from === selection.to) {
                    setLinkText(result.title)
                  }
                }}
              />
            )}
            <div className="space-y-2">
              <label htmlFor={linkUrlId} className="text-sm font-medium leading-none">
                {copy.url}
              </label>
              <input
                ref={urlInputRef}
                id={linkUrlId}
                value={linkUrl}
                onChange={event => {
                  setLinkUrl(event.target.value)
                  if (linkError) setLinkError('')
                }}
                placeholder="https://example.com"
                inputMode="url"
                autoComplete="url"
                aria-invalid={linkError ? true : undefined}
                aria-describedby={linkError ? `${linkUrlId}-error` : undefined}
                className={linkInputClass}
                autoFocus
              />
              {linkError && (
                <p id={`${linkUrlId}-error`} role="alert" className="text-xs text-destructive">
                  {linkError}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <label htmlFor={linkTextId} className="text-sm font-medium leading-none">
                {copy.text}
              </label>
              <input
                id={linkTextId}
                value={linkText}
                onChange={event => setLinkText(event.target.value)}
                placeholder={copy.textPlaceholder}
                readOnly={Boolean(snapshot?.active) || snapshot?.from !== snapshot?.to}
                className={linkInputClass}
              />
              <p className="text-xs text-foreground-muted">{copy.textHint}</p>
            </div>
          </div>
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-between">
            {snapshot?.active ? (
              <button
                type="button"
                className="inline-flex h-9 items-center justify-center rounded-[var(--radius-md)] px-3 text-sm font-medium text-destructive transition-token hover:bg-destructive/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                onClick={removeLink}
                disabled={!editor?.isEditable}
              >
                {copy.remove}
              </button>
            ) : (
              <span aria-hidden />
            )}
            <div className="flex flex-col-reverse gap-2 sm:flex-row">
              <button type="button" className={linkButtonClass} onClick={() => closePopup('cancel')}>
                {copy.cancel}
              </button>
              <button
                type="button"
                className="inline-flex h-9 items-center justify-center gap-2 rounded-[var(--radius-md)] border border-primary bg-primary px-4 text-sm font-medium text-primary-foreground shadow-sm transition-token hover:bg-primary/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-50"
                onClick={applyLink}
                disabled={!editor?.isEditable}
              >
                {snapshot?.active ? copy.saveButton : copy.insertButton}
              </button>
            </div>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

export interface MarkdownToolbarButtonProps
  extends Omit<ComponentPropsWithoutRef<'button'>, 'children' | 'onClick' | 'type'> {
  label: string
  active?: boolean
  disabled?: boolean
  onClick: () => void
  children: ReactNode
}

/** A formatting control that keeps the editor selection while it is clicked. */
export function MarkdownToolbarButton({
  label,
  active = false,
  disabled = false,
  onClick,
  children,
  className,
  ...props
}: MarkdownToolbarButtonProps) {
  return (
    <button
      {...props}
      type="button"
      aria-label={label}
      aria-pressed={active}
      disabled={disabled}
      tabIndex={-1}
      data-markdown-toolbar-button
      title={label}
      onMouseDown={event => event.preventDefault()}
      onClick={onClick}
      className={joinClasses(
        'inline-flex h-8 w-8 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50',
        active ? 'bg-surface-selected text-surface-selected-foreground' : undefined,
        className,
      )}
    >
      <span aria-hidden="true" className="contents">
        {children}
      </span>
    </button>
  )
}

export interface MarkdownToolbarGroupProps {
  label: string
  children: ReactNode
  className?: string
}

export function MarkdownToolbarGroup({
  label,
  children,
  className,
}: MarkdownToolbarGroupProps) {
  return (
    <div
      className={joinClasses(
        'inline-flex items-center gap-0.5 border-r border-border pr-1.5 last:border-r-0 last:pr-0',
        className,
      )}
      role="group"
      aria-label={label}
    >
      {children}
    </div>
  )
}

export interface MarkdownToolbarProps {
  editor: Editor | null
  children?: ReactNode
  className?: string
  'aria-label'?: string
  link?: MarkdownToolbarLinkOptions
  table?: MarkdownTableOptions
}

export interface MarkdownToolbarLinkOptions {
  disabled?: boolean
  normalizeUrl?: MarkdownLinkUrlNormalizer
  searchAdapter?: MarkdownSearchAdapter
  searchContext?: Omit<MarkdownSearchContext, 'signal'>
  searchLabels?: Partial<MarkdownLinkSearchLabels>
  searchClassName?: string
  labels?: Partial<MarkdownLinkLabels>
  popupClassName?: string
}

/**
 * The shared default Markdown formatting toolbar. Product integrations may
 * append controls as children; the default commands, active state, disabled
 * state, selection preservation, and roving keyboard focus stay here.
 */
export function MarkdownToolbar({
  editor,
  children,
  className,
  'aria-label': ariaLabel = 'Text formatting',
  link,
  table,
}: MarkdownToolbarProps) {
  const state = useMarkdownState(editor)
  const commands = useMarkdownCommands(editor)
  const toolbarRef = useRef<HTMLDivElement>(null)
  const [linkOpen, setLinkOpen] = useState(false)
  const imageUpload = useMarkdownImageUploadContext()
  const editable = Boolean(editor && state?.isEditable)
  const active = state?.active
  const linkLabels = { ...DEFAULT_MARKDOWN_LINK_LABELS, ...link?.labels }
  const tableLabels: MarkdownTableLabels = { ...DEFAULT_MARKDOWN_TABLE_LABELS, ...table?.labels }
  const linkDisabled = !editable || link?.disabled === true

  useLayoutEffect(() => {
    const buttons = toolbarRef.current?.querySelectorAll<HTMLButtonElement>(
      'button[data-markdown-toolbar-button]:not(:disabled)',
    )
    if (
      buttons?.length &&
      !Array.from(buttons).some(button => button.tabIndex === 0)
    ) {
      buttons[0]?.setAttribute('tabindex', '0')
    }
  })

  const handleToolbarKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (
      !(event.target instanceof HTMLButtonElement) ||
      !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)
    ) {
      return
    }

    const buttons = Array.from(
      toolbarRef.current?.querySelectorAll<HTMLButtonElement>(
        'button[data-markdown-toolbar-button]:not(:disabled)',
      ) ?? [],
    )
    if (!buttons.length) return

    event.preventDefault()
    const currentIndex = Math.max(
      0,
      buttons.indexOf(event.target as HTMLButtonElement),
    )
    const nextIndex =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? buttons.length - 1
          : (currentIndex +
              (event.key === 'ArrowRight' ? 1 : -1) +
              buttons.length) %
            buttons.length
    buttons.forEach(button => {
      button.tabIndex = -1
    })
    buttons[nextIndex]?.setAttribute('tabindex', '0')
    buttons[nextIndex]?.focus()
  }

  const toggleHeading = (level: MarkdownHeadingLevel, isActive: boolean) => {
    if (isActive) commands.setParagraph()
    else commands.toggleHeading(level)
  }

  return (
    <div
      ref={toolbarRef}
      contentEditable={false}
      role="toolbar"
      aria-label={ariaLabel}
      aria-orientation="horizontal"
      onFocusCapture={event => {
        const target = event.target
        if (!(target instanceof HTMLButtonElement)) return
        toolbarRef.current
          ?.querySelectorAll<HTMLButtonElement>('button[data-markdown-toolbar-button]')
          .forEach(button => {
            button.tabIndex = button === target ? 0 : -1
          })
      }}
      onKeyDown={handleToolbarKeyDown}
      className={joinClasses(
        'sticky top-0 z-10 flex flex-wrap items-center gap-1.5 border-b border-border select-none',
        className ?? 'rounded-t-[var(--radius-sm)] bg-surface px-2 py-1.5',
      )}
    >
      <MarkdownToolbarGroup label="Block type">
        <MarkdownToolbarButton
          label="Paragraph"
          active={Boolean(active?.paragraph)}
          disabled={!editable}
          onClick={() => commands.setParagraph()}
        >
          <Pilcrow className="h-4 w-4" />
        </MarkdownToolbarButton>
        <MarkdownToolbarButton
          label="Heading 1"
          active={Boolean(active?.heading1)}
          disabled={!editable}
          onClick={() => toggleHeading(1, Boolean(active?.heading1))}
        >
          <Heading1 className="h-4 w-4" />
        </MarkdownToolbarButton>
        <MarkdownToolbarButton
          label="Heading 2"
          active={Boolean(active?.heading2)}
          disabled={!editable}
          onClick={() => toggleHeading(2, Boolean(active?.heading2))}
        >
          <Heading2 className="h-4 w-4" />
        </MarkdownToolbarButton>
        <MarkdownToolbarButton
          label="Heading 3"
          active={Boolean(active?.heading3)}
          disabled={!editable}
          onClick={() => toggleHeading(3, Boolean(active?.heading3))}
        >
          <Heading3 className="h-4 w-4" />
        </MarkdownToolbarButton>
      </MarkdownToolbarGroup>
      <MarkdownToolbarGroup label="Marks">
        <MarkdownToolbarButton
          label="Bold"
          active={Boolean(active?.bold)}
          disabled={!editable}
          onClick={() => commands.toggleBold()}
        >
          <Bold className="h-4 w-4" />
        </MarkdownToolbarButton>
        <MarkdownToolbarButton
          label="Italic"
          active={Boolean(active?.italic)}
          disabled={!editable}
          onClick={() => commands.toggleItalic()}
        >
          <Italic className="h-4 w-4" />
        </MarkdownToolbarButton>
        <MarkdownToolbarButton
          label="Strikethrough"
          active={Boolean(active?.strike)}
          disabled={!editable}
          onClick={() => commands.toggleStrike()}
        >
          <Strikethrough className="h-4 w-4" />
        </MarkdownToolbarButton>
        <MarkdownToolbarButton
          label="Inline code"
          active={Boolean(active?.code)}
          disabled={!editable}
          onClick={() => commands.toggleCode()}
        >
          <Code className="h-4 w-4" />
        </MarkdownToolbarButton>
      </MarkdownToolbarGroup>
      <MarkdownToolbarGroup label="Lists">
        <MarkdownToolbarButton
          label="Bulleted list"
          active={Boolean(active?.bulletList)}
          disabled={!editable}
          onClick={() => commands.toggleBulletList()}
        >
          <List className="h-4 w-4" />
        </MarkdownToolbarButton>
        <MarkdownToolbarButton
          label="Numbered list"
          active={Boolean(active?.orderedList)}
          disabled={!editable}
          onClick={() => commands.toggleOrderedList()}
        >
          <ListOrdered className="h-4 w-4" />
        </MarkdownToolbarButton>
      </MarkdownToolbarGroup>
      <MarkdownToolbarGroup label="Blocks">
        <MarkdownToolbarButton
          label="Blockquote"
          active={Boolean(active?.blockquote)}
          disabled={!editable}
          onClick={() => commands.toggleBlockquote()}
        >
          <Quote className="h-4 w-4" />
        </MarkdownToolbarButton>
        <MarkdownToolbarButton
          label="Code block"
          active={Boolean(active?.codeBlock)}
          disabled={!editable}
          onClick={() => commands.toggleCodeBlock()}
        >
          <Code2 className="h-4 w-4" />
        </MarkdownToolbarButton>
        <MarkdownToolbarButton
          label="Horizontal rule"
          disabled={!editable}
          onClick={() => commands.setHorizontalRule()}
        >
          <Minus className="h-4 w-4" />
        </MarkdownToolbarButton>
      </MarkdownToolbarGroup>
      <MarkdownToolbarGroup label="Insert">
        <MarkdownToolbarButton
          label={tableLabels.insertTable}
          disabled={!editable || !state?.table.canInsert}
          onClick={() => commands.insertTable()}
        >
          <Table className="h-4 w-4" />
        </MarkdownToolbarButton>
        <MarkdownToolbarButton
          label={active?.link ? linkLabels.editButton : linkLabels.insertButton}
          active={Boolean(active?.link)}
          disabled={linkDisabled}
          onClick={() => setLinkOpen(true)}
        >
          <Link2 className="h-4 w-4" />
        </MarkdownToolbarButton>
      </MarkdownToolbarGroup>
      {imageUpload && (
        <MarkdownToolbarGroup label={imageUpload.labels.group}>
          <MarkdownToolbarButton
            label={imageUpload.state.uploading ? imageUpload.labels.uploading : imageUpload.labels.insert}
            disabled={!editable || imageUpload.state.uploading}
            onClick={imageUpload.openPicker}
          >
            {imageUpload.state.uploading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <ImagePlus className="h-4 w-4" />
            )}
          </MarkdownToolbarButton>
        </MarkdownToolbarGroup>
      )}
      <MarkdownToolbarGroup label="History">
        <MarkdownToolbarButton
          label="Undo"
          disabled={!editable || !state?.canUndo}
          onClick={() => commands.undo()}
        >
          <Undo2 className="h-4 w-4" />
        </MarkdownToolbarButton>
        <MarkdownToolbarButton
          label="Redo"
          disabled={!editable || !state?.canRedo}
          onClick={() => commands.redo()}
        >
          <Redo2 className="h-4 w-4" />
        </MarkdownToolbarButton>
      </MarkdownToolbarGroup>
      {children}
      <MarkdownLinkPopup
        editor={editor}
        open={linkOpen}
        onOpenChange={setLinkOpen}
        normalizeUrl={link?.normalizeUrl}
        searchAdapter={link?.searchAdapter}
        searchContext={link?.searchContext}
        searchLabels={link?.searchLabels}
        searchClassName={link?.searchClassName}
        labels={link?.labels}
        className={link?.popupClassName}
      />
    </div>
  )
}
