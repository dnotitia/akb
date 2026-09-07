import { useEffect, useMemo, useState } from 'react'
import type { ComponentPropsWithoutRef, ReactNode } from 'react'
import { EditorContent, useEditor } from '@tiptap/react'
import type { Editor } from '@tiptap/core'

import { createMarkdownExtensions } from '../extensions.js'
import { markdownCommands } from '../core.js'
import type {
  MarkdownCommands,
  MarkdownEditorConfig,
  MarkdownProfile,
  MarkdownSlashContext,
  MarkdownState,
} from '../types.js'

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

interface MarkdownSurfaceProps extends Omit<ComponentPropsWithoutRef<'div'>, 'onChange'> {
  editor: Editor | null
  editable: boolean
  children?: ReactNode
}

function MarkdownSurface({ editor, editable, children, ...props }: MarkdownSurfaceProps) {
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
}

export function MarkdownEditor({
  markdown,
  profile = 'preserve',
  onChange,
  onSlash,
  ...props
}: MarkdownEditorProps) {
  const editor = useMarkdownEditor({
    initialMarkdown: markdown,
    profile,
    editable: true,
    onChange,
    onSlash,
  })

  useEffect(() => {
    if (!editor || editor.getMarkdown() === markdown) {
      return
    }

    editor.commands.setContent(markdown, { contentType: 'markdown' })
  }, [editor, markdown])

  return <MarkdownSurface {...props} editor={editor} editable />
}

export interface MarkdownViewerProps extends Omit<MarkdownSurfaceProps, 'editor' | 'editable'> {
  markdown: string
  profile?: MarkdownProfile
}

export function MarkdownViewer({
  markdown,
  profile = 'preserve',
  ...props
}: MarkdownViewerProps) {
  const editor = useMarkdownEditor({
    initialMarkdown: markdown,
    profile,
    editable: false,
  })

  useEffect(() => {
    if (!editor || editor.getMarkdown() === markdown) {
      return
    }

    editor.commands.setContent(markdown, { contentType: 'markdown' })
  }, [editor, markdown])

  return <MarkdownSurface {...props} editor={editor} editable={false} />
}

export { EditorContent }
