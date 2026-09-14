import type { Editor, EditorOptions, FocusPosition, JSONContent } from '@tiptap/core'
import type { MarkdownExtensionOptions } from '@tiptap/markdown'

export type MarkdownProfile = 'structured' | 'preserve'

export type MarkdownTargetKind = 'document' | 'file' | 'attachment'

/**
 * A resource reference as it appears in canonical Markdown.  `target` is the
 * durable value; a resolver's short-lived runtime URL is deliberately not a
 * field on this type.
 */
export interface MarkdownTarget {
  kind: MarkdownTargetKind
  target: string
}

export type MarkdownUnavailableReason =
  | 'inaccessible'
  | 'deleted'
  | 'unsupported'
  | 'cross-vault'
  | 'expired'
  | 'unknown'

export type MarkdownTargetResolution =
  | {
      target: string
      kind?: MarkdownTargetKind
      status: 'available'
      /** Short-lived runtime URL; never serialized to Markdown. */
      runtimeUrl: string
      label?: string
    }
  | {
      target: string
      kind?: MarkdownTargetKind
      status: 'unavailable'
      reason: MarkdownUnavailableReason
      label?: string
    }

export interface MarkdownParseOptions {
  profile?: MarkdownProfile
  markedOptions?: MarkdownExtensionOptions['markedOptions']
}

export interface MarkdownUploadContext {
  vault?: string
  documentId?: string
  draftId?: string
  target?: string
  signal?: AbortSignal
}

export interface MarkdownAsset {
  /** Opaque product identity used for draft handoff/cleanup, when provided. */
  id?: string
  /** Canonical target to write into Markdown. */
  target: string
  kind: 'file' | 'attachment'
  alt?: string
  title?: string
  /** Server-provided expiry for an unclaimed upload, when applicable. */
  expiresAt?: string
}

export interface MarkdownUploadAdapter {
  upload(file: Blob, context?: MarkdownUploadContext): Promise<MarkdownAsset>
}

export interface MarkdownAdapterError {
  code: 'unavailable' | 'invalid' | 'conflict' | 'cancelled' | 'unknown'
  message: string
  retryable: boolean
}

export type MarkdownUploadItem =
  | { status: 'success'; file: Blob; asset: MarkdownAsset }
  | { status: 'failed'; file: Blob; error: MarkdownAdapterError }
  | { status: 'cancelled'; file: Blob; error: MarkdownAdapterError }

export interface MarkdownUploadBatchResult {
  items: readonly MarkdownUploadItem[]
  succeeded: number
  failed: number
  cancelled: number
  partial: boolean
}

export interface MarkdownSearchResult {
  id: string
  title: string
  snippet?: string
  /** Canonical target to insert into Markdown; never a signed/runtime URL. */
  target: string
  kind?: MarkdownTargetKind
}

export interface MarkdownSearchAdapter {
  search(query: string, context?: MarkdownSearchContext): Promise<readonly MarkdownSearchResult[]>
}

export interface MarkdownSearchContext {
  vault?: string
  signal?: AbortSignal
}

export interface MarkdownTargetResolver {
  resolve(
    target: string,
    context?: MarkdownTargetResolverContext,
  ): Promise<MarkdownTargetResolution>
}

export interface MarkdownTargetResolverContext {
  vault?: string
  document?: string
  commit?: string
  signal?: AbortSignal
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
  adapters?: MarkdownAdapters
  onChange?: (markdown: string, editor: Editor) => void
  onSlash?: (context: MarkdownSlashContext) => void
}

export interface MarkdownCommands {
  setMarkdown(markdown: string): boolean
  insertMarkdown(markdown: string): boolean
  insertImage(target: string, alt?: string, title?: string): boolean
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
