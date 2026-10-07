import type {
  MarkdownEditorHandle,
  MarkdownSourceChange,
  MarkdownSourceChangeResult,
  MarkdownSourceMode,
  MarkdownSourceSelection,
  MarkdownSourceState,
} from '../types.js'

interface HistoryEntry {
  markdown: string
  selection: MarkdownSourceSelection
}

type SourceChangeHandler = (markdown: string, editor: MarkdownEditorHandle) => void

const EMPTY_SELECTION: MarkdownSourceSelection = {
  start: 0,
  end: 0,
  direction: 'none',
}

const sessions = new WeakMap<MarkdownEditorHandle, MarkdownSourceSession>()

function clamp(value: number, maximum: number): number {
  return Math.max(0, Math.min(value, maximum))
}

function normalizeSelection(
  selection: MarkdownSourceSelection,
  markdown: string,
): MarkdownSourceSelection {
  const start = clamp(Math.min(selection.start, selection.end), markdown.length)
  const end = clamp(Math.max(selection.start, selection.end), markdown.length)
  return { start, end, direction: selection.direction }
}

function mapPosition(position: number, from: number, to: number, insertedLength: number): number {
  if (position < from) return position
  if (position > to) return position + insertedLength - (to - from)
  if (from === to) return from + insertedLength
  if (position === from) return from
  if (position === to) return from + insertedLength
  return from + Math.min(position - from, insertedLength)
}

function isGroupedInput(
  previous: HistoryEntry,
  nextMarkdown: string,
  nextSelection: MarkdownSourceSelection,
  inputType: string,
): boolean {
  const { markdown, selection } = previous
  if (
    selection.start !== selection.end ||
    nextSelection.start !== nextSelection.end
  ) return false

  if (inputType === 'insertText') {
    const from = selection.start
    const added = nextSelection.start - from
    return added > 0 &&
      nextMarkdown.length === markdown.length + added &&
      nextMarkdown.slice(0, from) === markdown.slice(0, from) &&
      nextMarkdown.slice(nextSelection.start) === markdown.slice(from)
  }

  if (inputType === 'deleteContentBackward') {
    const from = selection.start - 1
    return from >= 0 && nextSelection.start === from &&
      nextMarkdown === markdown.slice(0, from) + markdown.slice(selection.start)
  }

  if (inputType === 'deleteContentForward') {
    const to = selection.start + 1
    return nextSelection.start === selection.start &&
      nextMarkdown === markdown.slice(0, selection.start) + markdown.slice(to)
  }

  return false
}

export class MarkdownSourceSession {
  private readonly listeners = new Set<() => void>()
  private readonly history: HistoryEntry[]
  private historyIndex = 0
  private markdown: string
  private selection: MarkdownSourceSelection = EMPTY_SELECTION
  private mode: MarkdownSourceMode = 'wysiwyg'
  private revision = 0
  private session = 0
  private composing = false
  private compositionStartMarkdown = ''
  private editable = true
  private attached = false
  private dirty = false
  private lastAppliedSource: string | null = null
  private lastInputType = ''
  private lastInputAt = 0
  private changeHandler?: SourceChangeHandler
  private inputElement: HTMLTextAreaElement | null = null
  private focusRequested = false
  private contextKey: string | undefined
  private snapshot: MarkdownSourceState

  constructor(
    private readonly handle: MarkdownEditorHandle,
    markdown: string,
    contextKey?: string,
  ) {
    this.markdown = markdown
    this.contextKey = contextKey
    this.history = [{ markdown, selection: this.selection }]
    this.snapshot = this.createSnapshot()
  }

  get isAttached(): boolean {
    return this.attached
  }

  get hasPendingChanges(): boolean {
    return this.dirty
  }

  getSnapshot = (): MarkdownSourceState => this.snapshot

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  setChangeHandler(handler: SourceChangeHandler | undefined): void {
    this.changeHandler = handler
  }

  setInputElement(input: HTMLTextAreaElement | null): void {
    this.inputElement = input
  }

  takeFocusRequest(): boolean {
    const requested = this.focusRequested
    this.focusRequested = false
    return requested
  }

  setEditable(editable: boolean): void {
    if (this.editable === editable) return
    this.editable = editable
    this.session += 1
    this.publish()
  }

  attach(): void {
    if (this.attached) return
    this.attached = true
    this.session += 1
    this.publish()
  }

  detach(): void {
    if (!this.attached) return
    this.attached = false
    this.mode = 'wysiwyg'
    this.composing = false
    this.editable = false
    this.dirty = false
    this.lastAppliedSource = null
    this.changeHandler = undefined
    this.inputElement = null
    this.focusRequested = false
    this.session += 1
    this.publish()
  }

  resetForSurface(markdown: string): void {
    this.markdown = markdown
    this.selection = EMPTY_SELECTION
    this.mode = 'wysiwyg'
    this.composing = false
    this.editable = true
    this.dirty = false
    this.lastAppliedSource = null
    this.revision += 1
    this.session += 1
    this.history.splice(0, this.history.length, { markdown, selection: this.selection })
    this.historyIndex = 0
    this.publish()
  }

  setContextKey(contextKey: string | undefined): void {
    if (this.contextKey === contextKey) return
    this.contextKey = contextKey
    this.selection = EMPTY_SELECTION
    this.revision += 1
    this.session += 1
    this.composing = false
    this.dirty = false
    this.lastAppliedSource = null
    this.history.splice(0, this.history.length, { markdown: this.markdown, selection: this.selection })
    this.historyIndex = 0
    this.lastInputType = ''
    this.lastInputAt = 0
    this.publish()
  }

  syncExternalValue(markdown: string): boolean {
    if (markdown === this.markdown) return false
    if (this.mode === 'source' && (this.dirty || this.composing)) {
      this.revision += 1
      this.session += 1
      this.publish()
      return false
    }

    this.markdown = markdown
    this.selection = normalizeSelection(this.selection, markdown)
    this.revision += 1
    this.dirty = false
    this.lastAppliedSource = null
    this.history.splice(0, this.history.length, { markdown, selection: this.selection })
    this.historyIndex = 0
    this.lastInputType = ''
    this.lastInputAt = 0
    this.publish()
    return true
  }

  enterSource(markdown: string): void {
    if (this.mode === 'source') return
    const sourceWasAppliedWithoutWysiwygChanges =
      !this.dirty && this.lastAppliedSource !== null && this.lastAppliedSource === this.markdown
    if (!sourceWasAppliedWithoutWysiwygChanges && this.markdown !== markdown) {
      this.resetDraft(markdown)
    }
    this.mode = 'source'
    this.session += 1
    this.composing = false
    this.publish()
  }

  leaveSource(): void {
    if (this.mode !== 'source') return
    this.mode = 'wysiwyg'
    this.composing = false
    this.session += 1
    this.publish()
  }

  markApplied(): void {
    this.dirty = false
    this.lastAppliedSource = this.markdown
  }

  setSelection(selection: MarkdownSourceSelection): void {
    const next = normalizeSelection(selection, this.markdown)
    if (
      next.start === this.selection.start &&
      next.end === this.selection.end &&
      next.direction === this.selection.direction
    ) return
    this.selection = next
    if (!this.composing) this.replaceCurrentHistorySelection(next)
    this.publish()
  }

  startComposition(): void {
    if (this.composing || this.mode !== 'source' || !this.editable) return
    this.composing = true
    this.compositionStartMarkdown = this.markdown
    this.publish()
  }

  endComposition(selection: MarkdownSourceSelection): void {
    if (!this.composing) return
    this.composing = false
    this.selection = normalizeSelection(selection, this.markdown)
    if (this.markdown !== this.compositionStartMarkdown) {
      this.pushHistory(this.markdown, this.selection, false)
      this.dirty = true
      this.lastInputType = ''
      this.lastInputAt = 0
    } else this.replaceCurrentHistorySelection(this.selection)
    this.publish()
  }

  input(
    markdown: string,
    selection: MarkdownSourceSelection,
    inputType: string,
  ): boolean {
    if (!this.attached || this.mode !== 'source' || !this.editable || markdown === this.markdown) {
      return false
    }
    this.markdown = markdown
    this.selection = normalizeSelection(selection, markdown)
    this.revision += 1
    this.dirty = true
    if (!this.composing) {
      const now = Date.now()
      const previous = this.history[this.historyIndex]
      const group = previous !== undefined && inputType === this.lastInputType &&
        now - this.lastInputAt <= 1000 &&
        isGroupedInput(previous, markdown, this.selection, inputType)
      this.pushHistory(markdown, this.selection, group)
      this.lastInputType = inputType
      this.lastInputAt = now
    }
    this.publish()
    this.changeHandler?.(markdown, this.handle)
    return true
  }

  apply(change: MarkdownSourceChange): MarkdownSourceChangeResult {
    if (!this.attached || this.mode !== 'source') return { applied: false, reason: 'source-inactive' }
    if (!this.editable) return { applied: false, reason: 'read-only' }
    if (this.composing) return { applied: false, reason: 'composing' }
    if (change.revision !== this.revision || change.session !== this.session) {
      return { applied: false, reason: 'stale' }
    }
    if (
      !Number.isInteger(change.from) ||
      !Number.isInteger(change.to) ||
      change.from < 0 ||
      change.to < change.from ||
      change.to > this.markdown.length
    ) return { applied: false, reason: 'invalid-range' }
    if (this.markdown.slice(change.from, change.to) !== change.expectedText) {
      return { applied: false, reason: 'target-mismatch' }
    }
    if (change.expectedText === change.replacementText) {
      return { applied: false, reason: 'unchanged' }
    }

    const nextMarkdown = this.markdown.slice(0, change.from) +
      change.replacementText + this.markdown.slice(change.to)
    this.selection = normalizeSelection({
      start: mapPosition(this.selection.start, change.from, change.to, change.replacementText.length),
      end: mapPosition(this.selection.end, change.from, change.to, change.replacementText.length),
      direction: this.selection.direction,
    }, nextMarkdown)
    this.markdown = nextMarkdown
    this.revision += 1
    this.dirty = true
    this.lastInputType = ''
    this.lastInputAt = 0
    this.pushHistory(nextMarkdown, this.selection, false)
    this.publish()
    this.changeHandler?.(nextMarkdown, this.handle)
    return { applied: true, markdown: nextMarkdown }
  }

  undo(): boolean {
    if (
      !this.attached || this.mode !== 'source' || !this.editable ||
      this.composing || this.historyIndex === 0
    ) return false
    this.historyIndex -= 1
    this.restoreHistoryEntry()
    return true
  }

  redo(): boolean {
    if (
      !this.attached || this.mode !== 'source' || !this.editable ||
      this.composing || this.historyIndex >= this.history.length - 1
    ) return false
    this.historyIndex += 1
    this.restoreHistoryEntry()
    return true
  }

  private resetDraft(markdown: string): void {
    this.markdown = markdown
    this.selection = normalizeSelection(this.selection, markdown)
    this.revision += 1
    this.dirty = false
    this.lastAppliedSource = null
    this.history.splice(0, this.history.length, { markdown, selection: this.selection })
    this.historyIndex = 0
    this.lastInputType = ''
    this.lastInputAt = 0
  }

  private pushHistory(
    markdown: string,
    selection: MarkdownSourceSelection,
    group: boolean,
  ): void {
    const entry = { markdown, selection }
    this.history.splice(this.historyIndex + 1)
    if (group && this.historyIndex > 0) {
      this.history[this.historyIndex] = entry
      return
    }
    this.history.push(entry)
    this.historyIndex = this.history.length - 1
  }

  private replaceCurrentHistorySelection(selection: MarkdownSourceSelection): void {
    const current = this.history[this.historyIndex]
    if (current?.markdown === this.markdown) this.history[this.historyIndex] = { ...current, selection }
  }

  private restoreHistoryEntry(): void {
    const entry = this.history[this.historyIndex]
    if (!entry) return
    this.markdown = entry.markdown
    this.selection = entry.selection
    this.revision += 1
    this.dirty = true
    this.focusRequested = true
    this.lastInputType = ''
    this.lastInputAt = 0
    this.publish()
    this.inputElement?.focus()
    this.changeHandler?.(this.markdown, this.handle)
  }

  private createSnapshot(): MarkdownSourceState {
    return Object.freeze({
      mode: this.mode,
      markdown: this.markdown,
      revision: this.revision,
      session: this.session,
      selection: Object.freeze({ ...this.selection }),
      isComposing: this.composing,
      canUndo: this.historyIndex > 0,
      canRedo: this.historyIndex < this.history.length - 1,
    })
  }

  private publish(): void {
    this.snapshot = this.createSnapshot()
    for (const listener of [...this.listeners]) listener()
  }
}

export function getMarkdownSourceSession(
  handle: MarkdownEditorHandle | null | undefined,
): MarkdownSourceSession | null {
  if (!handle) return null
  return sessions.get(handle) ?? null
}

export function getOrCreateMarkdownSourceSession(
  handle: MarkdownEditorHandle,
  markdown: string,
  contextKey?: string,
): MarkdownSourceSession {
  const current = sessions.get(handle)
  if (current) return current
  const session = new MarkdownSourceSession(handle, markdown, contextKey)
  sessions.set(handle, session)
  return session
}
