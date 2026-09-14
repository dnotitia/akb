import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ComponentPropsWithoutRef, ReactNode } from 'react'
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
  List,
  ListOrdered,
  Minus,
  Pilcrow,
  Quote,
  Redo2,
  Strikethrough,
  Undo2,
} from 'lucide-react'

import { createMarkdownExtensions } from '../extensions.js'
import { extractMarkdownTargets, markdownCommands } from '../core.js'
import { resolveMarkdownTargets } from '../adapters.js'
import type {
  MarkdownAdapters,
  MarkdownCommands,
  MarkdownEditorConfig,
  MarkdownHeadingLevel,
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
}: MarkdownToolbarProps) {
  const state = useMarkdownState(editor)
  const commands = useMarkdownCommands(editor)
  const toolbarRef = useRef<HTMLDivElement>(null)
  const editable = Boolean(editor && state?.isEditable)
  const active = state?.active

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
    </div>
  )
}
