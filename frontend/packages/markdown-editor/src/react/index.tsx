import { useEffect, useMemo, useState } from 'react'
import type { ComponentPropsWithoutRef, ReactNode } from 'react'
import { EditorContent, useEditor } from '@tiptap/react'
import type { Editor } from '@tiptap/core'

import { createMarkdownExtensions } from '../extensions.js'
import { extractMarkdownTargets, markdownCommands } from '../core.js'
import { resolveMarkdownTargets } from '../adapters.js'
import type {
  MarkdownAdapters,
  MarkdownCommands,
  MarkdownEditorConfig,
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
            toggleBold: () => false,
            toggleItalic: () => false,
            toggleBulletList: () => false,
            toggleOrderedList: () => false,
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
