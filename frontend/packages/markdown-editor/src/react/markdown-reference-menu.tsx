import { Extension, type Editor, type Range } from '@tiptap/core'
import { PluginKey } from '@tiptap/pm/state'
import { ReactRenderer } from '@tiptap/react'
import {
  exitSuggestion,
  findSuggestionMatch,
  Suggestion,
} from '@tiptap/suggestion'
import type {
  SuggestionKeyDownProps,
  SuggestionOptions,
  SuggestionPositionData,
  SuggestionProps,
} from '@tiptap/suggestion'
import { CircleDot, FileText, Folder, UserRound } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useEffect, useRef } from 'react'

import {
  getMarkdownSlashMenuBoundary,
  resolveMarkdownSlashMenuPosition,
} from './markdown-slash-command.js'
import type {
  MarkdownReferenceCandidate,
  MarkdownReferenceKind,
  MarkdownReferenceLabels,
  MarkdownReferenceOptions,
} from '../types.js'

export type {
  MarkdownReferenceAdapter,
  MarkdownReferenceCandidate,
  MarkdownReferenceContext,
  MarkdownReferenceKind,
  MarkdownReferenceLabels,
  MarkdownReferenceOptions,
} from '../types.js'

export const DEFAULT_MARKDOWN_REFERENCE_LABELS: MarkdownReferenceLabels = {
  header: 'Insert reference',
  escapeHint: 'Esc',
  sections: {
    person: 'People',
    issue: 'Issues',
    document: 'Documents',
    file: 'Files',
  },
  searching: 'Searching…',
  empty: 'No matching references.',
  error: 'Unable to search references.',
  footer: {
    navigation: '↑↓ Navigate',
    insert: '↵ Insert',
    close: 'Esc Close',
  },
}

const MARKDOWN_REFERENCE_KIND_ORDER: readonly MarkdownReferenceKind[] = [
  'person',
  'issue',
  'document',
  'file',
]

const MARKDOWN_REFERENCE_KIND_ICONS: Record<MarkdownReferenceKind, LucideIcon> = {
  person: UserRound,
  issue: CircleDot,
  document: FileText,
  file: Folder,
}

const markdownReferencePluginKey = new PluginKey('markdownReference')

type MarkdownReferenceStatus = 'loading' | 'results' | 'empty' | 'error'

interface MarkdownReferenceMenuProps {
  items: readonly MarkdownReferenceCandidate[]
  selectedIndex: number
  listboxId: string
  labels: MarkdownReferenceLabels
  status: MarkdownReferenceStatus
  errorMessage?: string
  onActiveChange: (index: number) => void
  onSelect: (candidate: MarkdownReferenceCandidate) => void
}

function mergeMarkdownReferenceLabels(
  labels?: MarkdownReferenceOptions['labels'],
): MarkdownReferenceLabels {
  return {
    ...DEFAULT_MARKDOWN_REFERENCE_LABELS,
    ...labels,
    sections: {
      ...DEFAULT_MARKDOWN_REFERENCE_LABELS.sections,
      ...labels?.sections,
    },
    footer: {
      ...DEFAULT_MARKDOWN_REFERENCE_LABELS.footer,
      ...labels?.footer,
    },
  }
}

function candidateKey(candidate: MarkdownReferenceCandidate): string {
  return `${candidate.kind}:${candidate.id}`
}

function isReferenceKind(value: unknown): value is MarkdownReferenceKind {
  return (
    value === 'person' ||
    value === 'issue' ||
    value === 'document' ||
    value === 'file'
  )
}

/** Keep malformed adapter results out of the selectable menu. */
export function normalizeMarkdownReferenceCandidates(
  candidates: readonly MarkdownReferenceCandidate[],
): MarkdownReferenceCandidate[] {
  const seen = new Set<string>()
  const normalized: MarkdownReferenceCandidate[] = []

  for (const candidate of candidates) {
    if (
      !candidate ||
      typeof candidate.id !== 'string' ||
      !candidate.id ||
      typeof candidate.title !== 'string' ||
      !candidate.title ||
      !isReferenceKind(candidate.kind)
    ) {
      continue
    }
    if (
      (candidate.kind === 'document' || candidate.kind === 'file') &&
      (typeof candidate.target !== 'string' || !candidate.target)
    ) {
      continue
    }

    const key = candidateKey(candidate)
    if (seen.has(key)) continue
    seen.add(key)
    normalized.push(candidate)
  }

  return normalized
}

function contextKey(context: MarkdownReferenceOptions['context']): string {
  return [context?.vault ?? '', context?.document ?? '', context?.commit ?? ''].join('\u0000')
}

function referenceValue(candidate: MarkdownReferenceCandidate): string {
  if (candidate.value !== undefined) return candidate.value
  return candidate.kind === 'person' ? `@${candidate.id}` : candidate.id
}

function insertMarkdownReference(
  editor: Editor,
  range: Range,
  candidate: MarkdownReferenceCandidate,
): void {
  const content =
    candidate.kind === 'document' || candidate.kind === 'file'
      ? [
          {
            type: 'text' as const,
            text: candidate.title,
            marks: [{ type: 'link', attrs: { href: candidate.target } }],
          },
          { type: 'text' as const, text: ' ' },
        ]
      : [
          { type: 'text' as const, text: referenceValue(candidate) },
          { type: 'text' as const, text: ' ' },
        ]

  editor
    .chain()
    .focus()
    .deleteRange(range)
    .insertContent(content)
    .run()
}

function setMarkdownReferenceAria(
  editor: Editor,
  listboxId: string,
  selectedIndex: number,
  itemCount: number,
  open: boolean,
): void {
  const element = editor.view.dom
  if (!open) {
    if (element.getAttribute('aria-controls') === listboxId) {
      element.removeAttribute('aria-controls')
      element.removeAttribute('aria-activedescendant')
    }
    element.removeAttribute('aria-autocomplete')
    element.setAttribute('aria-expanded', 'false')
    return
  }

  element.setAttribute('aria-autocomplete', 'list')
  element.setAttribute('aria-controls', listboxId)
  element.setAttribute('aria-expanded', 'true')
  const selectedId = itemCount > 0 ? `${listboxId}-option-${selectedIndex}` : undefined
  if (selectedId) element.setAttribute('aria-activedescendant', selectedId)
  else element.removeAttribute('aria-activedescendant')
}

function ensureMarkdownReferenceOptionVisible(
  listboxId: string,
  selectedIndex: number,
): void {
  const option = document.getElementById(`${listboxId}-option-${selectedIndex}`)
  const options = option?.closest<HTMLElement>('.markdown-reference-options')
  if (!option || !options) return

  const optionTop = option.offsetTop
  const optionBottom = optionTop + option.offsetHeight
  if (optionTop < options.scrollTop) options.scrollTop = optionTop
  else if (optionBottom > options.scrollTop + options.clientHeight) {
    options.scrollTop = optionBottom - options.clientHeight
  }
}

function MarkdownReferenceMenu({
  items,
  selectedIndex,
  listboxId,
  labels,
  status,
  errorMessage,
  onActiveChange,
  onSelect,
}: MarkdownReferenceMenuProps) {
  const optionsRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const options = optionsRef.current
    if (!options) return
    const stopWheelPropagation = (event: WheelEvent) => event.stopPropagation()
    options.addEventListener('wheel', stopWheelPropagation)
    return () => options.removeEventListener('wheel', stopWheelPropagation)
  }, [])

  return (
    <div
      id={listboxId}
      role="listbox"
      aria-label={labels.header}
      aria-busy={status === 'loading'}
      data-testid="markdown-reference-menu"
      className="markdown-reference-menu"
    >
      <div className="markdown-reference-header">
        <span className="markdown-reference-header-label">{labels.header}</span>
        <kbd className="markdown-reference-escape">{labels.escapeHint}</kbd>
      </div>

      <div ref={optionsRef} className="markdown-reference-options">
        {status === 'loading' && (
          <div role="status" data-testid="markdown-reference-loading" className="markdown-reference-status">
            {labels.searching}
          </div>
        )}

        {status === 'error' && (
          <div role="alert" data-testid="markdown-reference-error" className="markdown-reference-status">
            {errorMessage || labels.error}
          </div>
        )}

        {status === 'empty' && (
          <div role="status" data-testid="markdown-reference-empty" className="markdown-reference-status">
            {labels.empty}
          </div>
        )}

        {status === 'results' &&
          MARKDOWN_REFERENCE_KIND_ORDER.map(kind => {
            const categoryItems = items
              .map((candidate, index) => ({ candidate, index }))
              .filter(({ candidate }) => candidate.kind === kind)
            if (categoryItems.length === 0) return null
            const Icon = MARKDOWN_REFERENCE_KIND_ICONS[kind]

            return (
              <section
                key={kind}
                aria-label={labels.sections[kind]}
                data-reference-section={kind}
                className="markdown-reference-section"
              >
                <div className="markdown-reference-section-label">{labels.sections[kind]}</div>
                {categoryItems.map(({ candidate, index }) => {
                  const selected = selectedIndex === index
                  const optionId = `${listboxId}-option-${index}`
                  const detail = candidate.subtitle || candidate.snippet

                  return (
                    <button
                      key={candidateKey(candidate)}
                      id={optionId}
                      type="button"
                      role="option"
                      tabIndex={-1}
                      aria-selected={selected}
                      aria-label={detail ? `${candidate.title}: ${detail}` : candidate.title}
                      data-reference-kind={candidate.kind}
                      data-reference-id={candidate.id}
                      className={[
                        'markdown-reference-option',
                        selected ? 'markdown-reference-option-selected' : '',
                      ]
                        .filter(Boolean)
                        .join(' ')}
                      onMouseDown={event => event.preventDefault()}
                      onPointerMove={() => onActiveChange(index)}
                      onClick={() => onSelect(candidate)}
                    >
                      <Icon className="markdown-reference-icon" aria-hidden="true" />
                      <span className="markdown-reference-copy">
                        <span className="markdown-reference-label" title={candidate.title}>
                          {candidate.title}
                        </span>
                        {detail && <span className="markdown-reference-description">{detail}</span>}
                      </span>
                    </button>
                  )
                })}
              </section>
            )
          })}
      </div>

      <div className="markdown-reference-footer" aria-hidden="true">
        <span>{labels.footer.navigation}</span>
        <span>{labels.footer.insert}</span>
        <span>{labels.footer.close}</span>
      </div>
    </div>
  )
}

function hasAnchorArea(rect: Pick<DOMRect, 'left' | 'right' | 'top' | 'bottom'>): boolean {
  return rect.right > rect.left || rect.bottom > rect.top
}

function getMarkdownReferenceAnchorRect(
  editor: Editor,
  fallback: (() => DOMRect | null) | null | undefined,
): DOMRect | null {
  const decoration = editor.view.dom.querySelector<HTMLElement>(
    '.markdown-reference-suggestion[data-decoration-id]',
  )
  const decorationRect = decoration?.getBoundingClientRect()
  if (decorationRect && hasAnchorArea(decorationRect)) return decorationRect
  return fallback?.() ?? null
}

function positionMarkdownReferenceMenu(
  menu: HTMLElement,
  editor: Editor,
  data: SuggestionPositionData,
  anchor: DOMRect | null,
): boolean {
  const editorRoot =
    editor.view.dom.closest<HTMLElement>('[data-testid="markdown-editor"]') ?? editor.view.dom
  const boundary = getMarkdownSlashMenuBoundary(editorRoot)
  const availableWidth = Math.max(0, boundary.right - boundary.left)
  const availableHeight = Math.max(0, boundary.bottom - boundary.top)
  const measuredRect = menu.getBoundingClientRect()
  const width = Math.min(
    Math.max(measuredRect.width, 360),
    availableWidth || Math.max(measuredRect.width, 360),
  )
  const height = Math.min(
    measuredRect.height || 240,
    availableHeight || 420,
  )

  if (availableWidth > 0) menu.style.width = `${width}px`
  if (availableHeight > 0) menu.style.maxHeight = `${Math.min(420, availableHeight)}px`

  const resolved = resolveMarkdownSlashMenuPosition({
    anchor,
    boundary,
    menuWidth: width,
    menuHeight: height,
    data,
  })
  if (!resolved.visible) {
    menu.style.visibility = 'hidden'
    return false
  }

  Object.assign(menu.style, {
    position: data.strategy,
    left: `${resolved.left}px`,
    top: `${resolved.top}px`,
    visibility: 'visible',
  })
  return true
}

interface MarkdownReferenceOptionsSource {
  getOptions: () => MarkdownReferenceOptions | false | undefined
  subscribe?: (listener: () => void) => () => void
}

function createMarkdownReferenceSuggestion(
  source: MarkdownReferenceOptionsSource,
): Omit<
  SuggestionOptions<MarkdownReferenceCandidate, MarkdownReferenceCandidate>,
  'editor'
> {
  let renderer: ReactRenderer<unknown, MarkdownReferenceMenuProps> | null = null
  let unmount: (() => void) | undefined
  let selectedIndex = 0
  let items: MarkdownReferenceCandidate[] = []
  let command: ((candidate: MarkdownReferenceCandidate) => void) | undefined
  let activeEditor: Editor | null = null
  let requestId = 0
  let requestQuery = ''
  let requestStatus: MarkdownReferenceStatus = 'empty'
  let requestError: string | undefined
  const listboxId = `markdown-reference-list-${Math.random().toString(36).slice(2, 10)}`

  source.subscribe?.(() => {
    requestId += 1
    if (activeEditor) {
      exitSuggestion(activeEditor.view, markdownReferencePluginKey)
      return
    }
    items = []
    command = undefined
    requestStatus = 'empty'
    requestError = undefined
  })

  const currentOptions = (): MarkdownReferenceOptions | undefined => {
    const options = source.getOptions()
    if (options === false || options === undefined) return undefined
    return options
  }
  const currentLabels = () => mergeMarkdownReferenceLabels(currentOptions()?.labels)

  async function searchReferences({
    query,
    signal,
  }: {
    query: string
    editor: Editor
    signal: AbortSignal
  }): Promise<MarkdownReferenceCandidate[]> {
    const options = currentOptions()
    const thisRequestId = ++requestId
    const thisContextKey = contextKey(options?.context)
    requestQuery = query
    requestStatus = 'loading'
    requestError = undefined
    if (!options) return []

    try {
      const results = await options.adapter.search(query, {
        ...options.context,
        signal,
      })
      if (signal.aborted || thisRequestId !== requestId) return []
      if (contextKey(currentOptions()?.context) !== thisContextKey) {
        return []
      }
      const normalized = normalizeMarkdownReferenceCandidates(results)
      requestStatus = normalized.length > 0 ? 'results' : 'empty'
      return normalized
    } catch (error) {
      if (signal.aborted || thisRequestId !== requestId) return []
      if (contextKey(currentOptions()?.context) !== thisContextKey) {
        return []
      }
      requestStatus = 'error'
      requestError = error instanceof Error && error.message ? error.message : undefined
      throw error
    }
  }

  function setActiveIndex(nextIndex: number): void {
    if (nextIndex < 0 || nextIndex >= items.length) return
    selectedIndex = nextIndex
    renderer?.updateProps({ selectedIndex })
    queueMicrotask(() => ensureMarkdownReferenceOptionVisible(listboxId, selectedIndex))
    if (activeEditor) {
      setMarkdownReferenceAria(activeEditor, listboxId, selectedIndex, items.length, true)
    }
  }

  function updateRenderer(
    props: SuggestionProps<MarkdownReferenceCandidate, MarkdownReferenceCandidate>,
  ): void {
    const labels = currentLabels()
    const nextItems = normalizeMarkdownReferenceCandidates(props.items)
    const status: MarkdownReferenceStatus = props.loading
      ? 'loading'
      : requestQuery === props.query
        ? requestStatus
        : nextItems.length > 0
          ? 'results'
          : 'empty'
    items = nextItems
    selectedIndex = 0
    command = props.command
    setMarkdownReferenceAria(props.editor, listboxId, selectedIndex, items.length, true)
    renderer?.updateProps({
      items,
      selectedIndex,
      listboxId,
      labels,
      status,
      errorMessage: requestError,
      onActiveChange: setActiveIndex,
      onSelect: (candidate: MarkdownReferenceCandidate) => command?.(candidate),
    })
    const optionsElement = renderer?.element.querySelector<HTMLElement>(
      '.markdown-reference-options',
    )
    if (optionsElement) optionsElement.scrollTop = 0
    queueMicrotask(() => ensureMarkdownReferenceOptionVisible(listboxId, selectedIndex))
  }

  return {
    pluginKey: markdownReferencePluginKey,
    char: '@',
    allowSpaces: false,
    allowedPrefixes: null,
    placement: 'bottom-start',
    flip: true,
    decorationClass: 'markdown-reference-suggestion',
    allow: ({ editor }) => {
      if (!currentOptions() || !editor.isEditable || editor.view.composing) return false
      if (editor.isActive('code') || editor.isActive('codeBlock') || editor.isActive('link')) {
        return false
      }
      return !editor.state.selection.$from.parent.type.spec.code
    },
    findSuggestionMatch: config => {
      const match = findSuggestionMatch({
        ...config,
        allowedPrefixes: null,
        startOfLine: false,
      })
      if (!match) return null

      const paragraphStart = config.$position.start()
      if (match.range.from <= paragraphStart) return match
      const previous = config.$position.doc.textBetween(
        match.range.from - 1,
        match.range.from,
        '\n',
      )
      return /^[\s([{"'`]$/.test(previous) ? match : null
    },
    items: searchReferences,
    command: ({ editor, range, props }) => insertMarkdownReference(editor, range, props),
    render: () => ({
      onStart: (
        props: SuggestionProps<MarkdownReferenceCandidate, MarkdownReferenceCandidate>,
      ) => {
        const options = currentOptions()
        options?.onOpenChange?.(true, () =>
          exitSuggestion(props.editor.view, markdownReferencePluginKey),
        )
        activeEditor = props.editor
        selectedIndex = 0
        items = normalizeMarkdownReferenceCandidates(props.items)
        command = props.command
        renderer = new ReactRenderer(MarkdownReferenceMenu, {
          editor: props.editor,
          className: options?.className ?? 'markdown-reference-popup',
          props: {
            items,
            selectedIndex,
            listboxId,
            labels: currentLabels(),
            status: props.loading ? 'loading' : requestStatus,
            errorMessage: requestError,
            onActiveChange: setActiveIndex,
            onSelect: (candidate: MarkdownReferenceCandidate) => command?.(candidate),
          },
        })
        renderer.element.dataset.testid = 'markdown-reference-popup'
        unmount = props.mount(renderer.element, {
          onPosition: data => {
            const positioned = positionMarkdownReferenceMenu(
              renderer?.element ?? document.body,
              props.editor,
              data,
              getMarkdownReferenceAnchorRect(props.editor, props.clientRect),
            )
            if (!positioned) exitSuggestion(props.editor.view, markdownReferencePluginKey)
          },
        })
        setMarkdownReferenceAria(props.editor, listboxId, selectedIndex, items.length, true)
      },
      onUpdate: updateRenderer,
      onExit: ({
        editor,
      }: SuggestionProps<MarkdownReferenceCandidate, MarkdownReferenceCandidate>) => {
        currentOptions()?.onOpenChange?.(false)
        setMarkdownReferenceAria(editor, listboxId, 0, items.length, false)
        unmount?.()
        unmount = undefined
        renderer?.destroy()
        renderer = null
        items = []
        command = undefined
        selectedIndex = 0
        activeEditor = null
        requestId += 1
        requestStatus = 'empty'
        requestError = undefined
      },
      onKeyDown: ({ event, view }: SuggestionKeyDownProps) => {
        if (event.isComposing) return false
        if (event.key === 'ArrowDown' && items.length > 0) {
          event.preventDefault()
          setActiveIndex((selectedIndex + 1) % items.length)
          return true
        }
        if (event.key === 'ArrowUp' && items.length > 0) {
          event.preventDefault()
          setActiveIndex((selectedIndex - 1 + items.length) % items.length)
          return true
        }
        if (event.key === 'Enter' && items.length > 0) {
          event.preventDefault()
          const selected = items[selectedIndex]
          if (!selected) return false
          command?.(selected)
          return true
        }
        if (event.key === 'Escape') {
          event.preventDefault()
          event.stopPropagation()
          exitSuggestion(view, markdownReferencePluginKey)
          return true
        }
        return false
      },
    }),
  }
}

function createMarkdownReferenceExtensionFromSource(
  source: MarkdownReferenceOptionsSource,
): Extension {
  const suggestion = createMarkdownReferenceSuggestion(source)
  return Extension.create({
    name: 'markdownReference',
    addProseMirrorPlugins() {
      return [Suggestion({ ...suggestion, editor: this.editor })]
    },
  })
}

export function createMarkdownReferenceExtension(
  options: MarkdownReferenceOptions,
): Extension {
  return createMarkdownReferenceExtensionFromSource({ getOptions: () => options })
}

/** Internal live bridge used by the React hook so context and lifecycle props stay current. */
export function createLiveMarkdownReferenceExtension(
  getOptions: () => MarkdownReferenceOptions | false | undefined,
  subscribe?: (listener: () => void) => () => void,
): Extension {
  return createMarkdownReferenceExtensionFromSource({ getOptions, subscribe })
}
