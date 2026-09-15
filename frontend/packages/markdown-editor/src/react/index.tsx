import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ComponentPropsWithoutRef, ReactNode } from 'react'
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
  Italic,
  Link2,
  List,
  ListOrdered,
  Minus,
  Pilcrow,
  Quote,
  Redo2,
  Strikethrough,
  Undo2,
  X,
} from 'lucide-react'

import { createMarkdownExtensions } from '../extensions.js'
import { extractMarkdownTargets, markdownCommands } from '../core.js'
import { normalizeMarkdownLinkUrl } from '../link.js'
import { resolveMarkdownTargets } from '../adapters.js'
import type {
  MarkdownAdapters,
  MarkdownCommands,
  MarkdownEditorConfig,
  MarkdownHeadingLevel,
  MarkdownLinkLabels,
  MarkdownLinkUrlNormalizer,
  MarkdownProfile,
  MarkdownSlashContext,
  MarkdownState,
  MarkdownTargetResolution,
  MarkdownTargetResolverContext,
} from '../types.js'

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
            setLink: () => false,
            insertLink: () => false,
            unsetLink: () => false,
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

export interface MarkdownEditorProps extends Omit<MarkdownSurfaceProps, 'editor' | 'editable'> {
  markdown: string
  profile?: MarkdownProfile
  onChange?: MarkdownEditorConfig['onChange']
  onSlash?: (context: MarkdownSlashContext) => void
  adapters?: MarkdownAdapters
  resolverContext?: MarkdownTargetResolverContext
}

export function MarkdownEditor({
  markdown,
  profile = 'preserve',
  onChange,
  onSlash,
  adapters,
  resolverContext,
  ...props
}: MarkdownEditorProps) {
  const editor = useMarkdownEditor({
    initialMarkdown: markdown,
    profile,
    editable: true,
    onChange,
    onSlash,
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
      editable
      resolutions={resolutions}
      resolvingTargets={Boolean(adapters?.targetResolver)}
    />
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
  /** Product-owned search UI; this ticket deliberately does not own search. */
  searchSlot?: (context: MarkdownLinkSearchSlotProps) => ReactNode
  labels?: Partial<MarkdownLinkLabels>
  className?: string
}

export interface MarkdownLinkSearchSlotProps {
  setUrl: (value: string) => void
  setText: (value: string) => void
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
  searchSlot,
  labels,
  className,
}: MarkdownLinkPopupProps) {
  const copy = { ...DEFAULT_MARKDOWN_LINK_LABELS, ...labels }
  const commands = useMarkdownCommands(editor)
  const urlInputRef = useRef<HTMLInputElement>(null)
  const snapshotRef = useRef<MarkdownLinkSelectionSnapshot | null>(null)
  const previousOpenRef = useRef(false)
  const closeReasonRef = useRef<'cancel' | 'commit'>('cancel')
  const [snapshot, setSnapshot] = useState<MarkdownLinkSelectionSnapshot | null>(null)
  const [linkUrl, setLinkUrl] = useState('')
  const [linkText, setLinkText] = useState('')
  const [linkError, setLinkError] = useState('')
  const linkUrlId = useId()
  const linkTextId = useId()

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
      requestAnimationFrame(() => urlInputRef.current?.focus())
    }

    if (!open && previousOpenRef.current && closeReasonRef.current === 'cancel') {
      restoreSelection()
    }

    previousOpenRef.current = open
  }, [editor, open, restoreSelection])

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
            requestAnimationFrame(() => urlInputRef.current?.focus())
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
            {searchSlot?.({ setUrl: setLinkUrl, setText: setLinkText })}
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
}

export interface MarkdownToolbarLinkOptions {
  disabled?: boolean
  normalizeUrl?: MarkdownLinkUrlNormalizer
  searchSlot?: (context: MarkdownLinkSearchSlotProps) => ReactNode
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
}: MarkdownToolbarProps) {
  const state = useMarkdownState(editor)
  const commands = useMarkdownCommands(editor)
  const toolbarRef = useRef<HTMLDivElement>(null)
  const [linkOpen, setLinkOpen] = useState(false)
  const editable = Boolean(editor && state?.isEditable)
  const active = state?.active
  const linkLabels = { ...DEFAULT_MARKDOWN_LINK_LABELS, ...link?.labels }
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
          label={active?.link ? linkLabels.editButton : linkLabels.insertButton}
          active={Boolean(active?.link)}
          disabled={linkDisabled}
          onClick={() => setLinkOpen(true)}
        >
          <Link2 className="h-4 w-4" />
        </MarkdownToolbarButton>
      </MarkdownToolbarGroup>
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
        searchSlot={link?.searchSlot}
        labels={link?.labels}
        className={link?.popupClassName}
      />
    </div>
  )
}
