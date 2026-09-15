import type { Editor, EditorOptions, FocusPosition, JSONContent } from '@tiptap/core'
import type { MarkdownExtensionOptions } from '@tiptap/markdown'
import type { MarkdownTableCommands, MarkdownTableState } from './table.js'

export type { MarkdownTableInsertionOptions, MarkdownTableState } from './table.js'

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

/**
 * Product policy for link destinations. The editor owns when this function is
 * called; products own any resource-specific canonicalization.
 */
export type MarkdownLinkUrlNormalizer = (raw: string) => string | null

export interface MarkdownLinkLabels {
  insertButton: string
  editButton: string
  saveButton: string
  insertTitle: string
  editTitle: string
  description: string
  url: string
  text: string
  textPlaceholder: string
  textHint: string
  cancel: string
  remove: string
  close: string
  invalidUrl: string
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

export type MarkdownHeadingLevel = 1 | 2 | 3

export interface MarkdownEditorConfig extends MarkdownParseOptions {
  initialMarkdown?: string
  editable?: boolean
  element?: EditorOptions['element']
  adapters?: MarkdownAdapters
  onChange?: (markdown: string, editor: Editor) => void
  onSlash?: (context: MarkdownSlashContext) => void
}

export interface MarkdownCommands extends MarkdownTableCommands {
  setMarkdown(markdown: string): boolean
  insertMarkdown(markdown: string): boolean
  insertImage(target: string, alt?: string, title?: string): boolean
  setLink(href: string): boolean
  insertLink(text: string, href: string): boolean
  unsetLink(): boolean
  setParagraph(): boolean
  toggleHeading(level: MarkdownHeadingLevel): boolean
  toggleBold(): boolean
  toggleItalic(): boolean
  toggleStrike(): boolean
  toggleCode(): boolean
  toggleBulletList(): boolean
  toggleOrderedList(): boolean
  toggleBlockquote(): boolean
  toggleCodeBlock(): boolean
  setHorizontalRule(): boolean
  undo(): boolean
  redo(): boolean
  focus(position?: FocusPosition): boolean
}

export interface MarkdownActiveState {
  paragraph: boolean
  heading1: boolean
  heading2: boolean
  heading3: boolean
  bold: boolean
  italic: boolean
  strike: boolean
  code: boolean
  bulletList: boolean
  orderedList: boolean
  blockquote: boolean
  codeBlock: boolean
  link: boolean
}

export interface MarkdownLinkState {
  active: boolean
  href: string
}

export interface MarkdownSelectionState {
  from: number
  to: number
}

export interface MarkdownState {
  markdown: string
  isEmpty: boolean
  isEditable: boolean
  table: MarkdownTableState
  active: MarkdownActiveState
  link: MarkdownLinkState
  canUndo: boolean
  canRedo: boolean
  selection: MarkdownSelectionState
}

export interface MarkdownDocument {
  type: 'doc'
  content?: JSONContent[]
}
