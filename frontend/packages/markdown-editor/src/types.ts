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
      /**
       * Optional expiry for the runtime URL. Consumers re-resolve before this
       * instant and keep the canonical target in the editor model.
       */
      expiresAt?: string
      label?: string
      /** Release short-lived runtime resources owned by this resolution. */
      release?: () => void
      /** Retry a runtime resource after a browser decode/request failure. */
      refresh?: (context?: MarkdownTargetRefreshContext) => Promise<MarkdownTargetResolution>
    }
  | {
      target: string
      kind?: MarkdownTargetKind
      status: 'unavailable'
      reason: MarkdownUnavailableReason
      label?: string
    }

export interface MarkdownTargetRefreshContext {
  signal?: AbortSignal
}

export interface MarkdownImageLabels {
  loading: (alt: string) => string
  unavailable: (alt: string) => string
}

export interface MarkdownImageClassNames {
  frame?: string
  image?: string
  message?: string
}

export interface MarkdownImageOptions {
  labels?: Partial<MarkdownImageLabels>
  classNames?: MarkdownImageClassNames
}

export interface MarkdownCodeLabels {
  /** Accessible name for the keyboard-focusable code scroll region. */
  region: (language?: string) => string
}

export interface MarkdownCodeOptions {
  labels?: Partial<MarkdownCodeLabels>
}

export interface MarkdownParseOptions {
  profile?: MarkdownProfile
  markedOptions?: MarkdownExtensionOptions['markedOptions']
}

export interface MarkdownUploadContext {
  vault?: string
  document?: string
  commit?: string
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

export interface MarkdownUploadBatchOptions {
  onFileStart?: (file: Blob, index: number, total: number) => void
  onFileSettled?: (item: MarkdownUploadItem, index: number, total: number) => void
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

export type MarkdownReferenceKind = 'person' | 'issue' | 'document' | 'file'

export type MarkdownInlineReferenceKind = Extract<MarkdownReferenceKind, 'person' | 'issue'>

export interface MarkdownReferenceToken {
  /** The product-owned entity kind represented by the stored token. */
  kind: MarkdownInlineReferenceKind
  /** Product identity used for adapter lookup, never a runtime URL. */
  id: string
  /** Exact canonical Markdown token as authored. */
  value: string
}

export type MarkdownReferenceUnavailableReason =
  | 'inaccessible'
  | 'deleted'
  | 'unsupported'
  | 'unknown'

export type MarkdownReferenceResolution =
  | {
      kind: MarkdownInlineReferenceKind
      id: string
      value: string
      status: 'available'
      /** Product display name; it is never serialized into Markdown. */
      title: string
      subtitle?: string
      /** Short-lived product route; it is never serialized into Markdown. */
      runtimeUrl?: string
    }
  | {
      kind: MarkdownInlineReferenceKind
      id: string
      value: string
      status: 'unavailable'
      reason: MarkdownReferenceUnavailableReason
      /** Optional product-provided explanation for accessible UI. */
      title?: string
    }

/**
 * A candidate returned by the product's common `@` reference search.
 *
 * `value` is the exact plain-text Markdown value to insert for people and
 * issues. Document and file candidates must provide a durable `target`; the
 * editor never turns a runtime or signed URL into stored Markdown.
 */
export interface MarkdownReferenceCandidate {
  id: string
  kind: MarkdownReferenceKind
  title: string
  subtitle?: string
  snippet?: string
  target?: string
  value?: string
}

export interface MarkdownReferenceContext extends MarkdownSearchContext {
  document?: string
  commit?: string
}

export interface MarkdownReferenceAdapter {
  search(
    query: string,
    context?: MarkdownReferenceContext,
  ): Promise<readonly MarkdownReferenceCandidate[]>
  /** Resolve one stored token without changing its canonical value. */
  resolve?(
    reference: MarkdownReferenceToken,
    context?: MarkdownReferenceContext,
  ): Promise<MarkdownReferenceResolution>
}

export interface MarkdownReferenceLabels {
  header: string
  escapeHint: string
  sections: Record<MarkdownReferenceKind, string>
  searching: string
  empty: string
  error: string
  footer: {
    navigation: string
    insert: string
    close: string
  }
}

export interface MarkdownReferenceOptions {
  adapter: MarkdownReferenceAdapter
  context?: Omit<MarkdownReferenceContext, 'signal'>
  labels?: {
    header?: string
    escapeHint?: string
    searching?: string
    empty?: string
    error?: string
    sections?: Partial<Record<MarkdownReferenceKind, string>>
    footer?: Partial<MarkdownReferenceLabels['footer']>
  }
  className?: string
  onOpenChange?: (open: boolean, dismiss?: () => void) => void
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
  reference?: MarkdownReferenceAdapter
  targetResolver?: MarkdownTargetResolver
}

export type MarkdownSlashCommandCategory = 'text' | 'lists' | 'structure'

export type MarkdownSlashCommandId =
  | 'heading1'
  | 'heading2'
  | 'heading3'
  | 'quote'
  | 'bulletList'
  | 'numberedList'
  | 'taskList'
  | 'table'
  | 'codeBlock'
  | 'divider'

export interface MarkdownSlashCommandMessages {
  header: string
  escapeHint: string
  sections: Record<MarkdownSlashCommandCategory, string>
  footer: {
    navigation: string
    insert: string
    close: string
  }
  empty: string
  commands: Record<
    MarkdownSlashCommandId,
    {
      label: string
      description: string
    }
  >
}

export interface MarkdownSlashCommandOptions {
  messages?: MarkdownSlashCommandMessages
  /** Keeps a surrounding Dialog/Sheet open while the menu consumes Escape. */
  onOpenChange?: (open: boolean, dismiss?: () => void) => void
}

export type MarkdownHeadingLevel = 1 | 2 | 3

export interface MarkdownEditorConfig extends MarkdownParseOptions {
  initialMarkdown?: string
  editable?: boolean
  element?: EditorOptions['element']
  adapters?: MarkdownAdapters
  onChange?: (markdown: string, editor: Editor) => void
}

export interface MarkdownCommands extends MarkdownTableCommands {
  setMarkdown(markdown: string): boolean
  insertMarkdown(markdown: string): boolean
  insertImage(target: string, alt?: string, title?: string): boolean
  /** Replace one image occurrence at this ProseMirror document position. */
  replaceImageAt(position: number, target: string, alt?: string, title?: string): boolean
  /** Update only the image node at this ProseMirror document position. */
  setImageAltAt(position: number, alt: string): boolean
  /** Remove only the image node at this ProseMirror document position. */
  deleteImageAt(position: number): boolean
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
  toggleTaskList(): boolean
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
  taskList: boolean
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
