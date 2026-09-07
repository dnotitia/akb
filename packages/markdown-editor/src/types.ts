import type { Editor, EditorOptions, FocusPosition, JSONContent } from '@tiptap/core'
import type { MarkdownExtensionOptions } from '@tiptap/markdown'

export type MarkdownProfile = 'structured' | 'preserve'

export interface MarkdownParseOptions {
  profile?: MarkdownProfile
  markedOptions?: MarkdownExtensionOptions['markedOptions']
}

export interface MarkdownUploadContext {
  documentId?: string
  target?: string
}

export interface MarkdownAsset {
  url: string
  alt?: string
  title?: string
}

export interface MarkdownUploadAdapter {
  upload(file: Blob, context?: MarkdownUploadContext): Promise<MarkdownAsset>
}

export interface MarkdownSearchResult {
  id: string
  title: string
  snippet?: string
  target?: string
}

export interface MarkdownSearchAdapter {
  search(query: string): Promise<readonly MarkdownSearchResult[]>
}

export interface MarkdownTargetResolver {
  resolve(target: string): Promise<string | null>
}

export interface MarkdownAdapters {
  upload?: MarkdownUploadAdapter
  search?: MarkdownSearchAdapter
  targetResolver?: MarkdownTargetResolver
}

export interface MarkdownSlashContext {
  editor: Editor
  position: number
}

export interface MarkdownEditorConfig extends MarkdownParseOptions {
  initialMarkdown?: string
  editable?: boolean
  element?: EditorOptions['element']
  onChange?: (markdown: string, editor: Editor) => void
  onSlash?: (context: MarkdownSlashContext) => void
}

export interface MarkdownCommands {
  setMarkdown(markdown: string): boolean
  insertMarkdown(markdown: string): boolean
  toggleBold(): boolean
  toggleItalic(): boolean
  toggleBulletList(): boolean
  toggleOrderedList(): boolean
  undo(): boolean
  redo(): boolean
  focus(position?: FocusPosition): boolean
}

export interface MarkdownSelectionState {
  from: number
  to: number
}

export interface MarkdownState {
  markdown: string
  isEmpty: boolean
  isEditable: boolean
  canUndo: boolean
  canRedo: boolean
  selection: MarkdownSelectionState
}

export interface MarkdownDocument {
  type: 'doc'
  content?: JSONContent[]
}
