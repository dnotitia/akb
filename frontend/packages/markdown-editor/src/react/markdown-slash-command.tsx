import { Extension, type ChainedCommands, type Editor, type Range } from '@tiptap/core'
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
import {
  CheckSquare,
  Code2,
  Heading1,
  Heading2,
  Heading3,
  List,
  ListOrdered,
  Minus,
  Quote,
  Table2,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useEffect, useRef } from 'react'

import type {
  MarkdownSlashCommandCategory,
  MarkdownSlashCommandId,
  MarkdownSlashCommandMessages,
  MarkdownSlashCommandOptions,
} from '../types.js'

export type {
  MarkdownSlashCommandCategory,
  MarkdownSlashCommandId,
  MarkdownSlashCommandMessages,
  MarkdownSlashCommandOptions,
} from '../types.js'

export const DEFAULT_MARKDOWN_SLASH_COMMAND_MESSAGES: MarkdownSlashCommandMessages = {
  header: 'Insert block',
  escapeHint: 'Esc',
  sections: {
    text: 'TEXT',
    lists: 'LISTS',
    structure: 'STRUCTURE',
  },
  footer: {
    navigation: '↑↓ Navigate',
    insert: '↵ Insert',
    close: 'Esc Close',
  },
  empty: 'No matching blocks.',
  commands: {
    heading1: { label: 'Heading 1', description: 'Large section heading' },
    heading2: { label: 'Heading 2', description: 'Medium section heading' },
    heading3: { label: 'Heading 3', description: 'Small section heading' },
    quote: { label: 'Quote', description: 'Call out a quotation' },
    bulletList: {
      label: 'Bullet list',
      description: 'Create an unordered list',
    },
    numberedList: {
      label: 'Numbered list',
      description: 'Create an ordered list',
    },
    taskList: { label: 'Task list', description: 'Track work with checkboxes' },
    table: { label: 'Table', description: 'Insert a basic 3 × 2 table' },
    codeBlock: { label: 'Code block', description: 'Add a fenced code block' },
    divider: { label: 'Divider', description: 'Separate sections with a rule' },
  },
}

export interface MarkdownSlashCommandDefinition {
  id: MarkdownSlashCommandId
  category: MarkdownSlashCommandCategory
  icon: LucideIcon
  keywords: readonly string[]
  action: (editor: Editor, range: Range) => void
}

export interface LocalizedMarkdownSlashCommand extends MarkdownSlashCommandDefinition {
  label: string
  description: string
}

const markdownSlashCommandPluginKey = new PluginKey('markdownSlashCommand')

function replaceSlashTrigger(
  editor: Editor,
  range: Range,
  command: (chain: ChainedCommands) => ChainedCommands,
): void {
  command(editor.chain().focus().deleteRange(range)).run()
}

/**
 * The common block registry owns filtering, rendering, and the Tiptap command
 * mapping. Products provide copy and styling around this registry; they do not
 * rebuild the editor command set.
 */
export const MARKDOWN_SLASH_COMMAND_DEFINITIONS: readonly MarkdownSlashCommandDefinition[] = [
  {
    id: 'heading1',
    category: 'text',
    icon: Heading1,
    keywords: ['heading', 'h1', 'title', '제목', '큰 제목'],
    action: (editor, range) =>
      replaceSlashTrigger(editor, range, chain => chain.setHeading({ level: 1 })),
  },
  {
    id: 'heading2',
    category: 'text',
    icon: Heading2,
    keywords: ['heading', 'h2', 'subtitle', '제목', '중간 제목'],
    action: (editor, range) =>
      replaceSlashTrigger(editor, range, chain => chain.setHeading({ level: 2 })),
  },
  {
    id: 'heading3',
    category: 'text',
    icon: Heading3,
    keywords: ['heading', 'h3', 'subtitle', '제목', '작은 제목'],
    action: (editor, range) =>
      replaceSlashTrigger(editor, range, chain => chain.setHeading({ level: 3 })),
  },
  {
    id: 'quote',
    category: 'text',
    icon: Quote,
    keywords: ['quote', 'blockquote', '인용', '인용문'],
    action: (editor, range) =>
      replaceSlashTrigger(editor, range, chain => chain.toggleBlockquote()),
  },
  {
    id: 'bulletList',
    category: 'lists',
    icon: List,
    keywords: ['bullet', 'bulleted', 'unordered', 'list', '글머리', '목록'],
    action: (editor, range) =>
      replaceSlashTrigger(editor, range, chain => chain.toggleBulletList()),
  },
  {
    id: 'numberedList',
    category: 'lists',
    icon: ListOrdered,
    keywords: ['number', 'numbered', 'ordered', 'list', '번호', '순서', '목록'],
    action: (editor, range) =>
      replaceSlashTrigger(editor, range, chain => chain.toggleOrderedList()),
  },
  {
    id: 'taskList',
    category: 'lists',
    icon: CheckSquare,
    keywords: ['task', 'todo', 'checklist', 'checkbox', '할 일', '체크리스트'],
    action: (editor, range) =>
      replaceSlashTrigger(editor, range, chain => chain.toggleTaskList()),
  },
  {
    id: 'table',
    category: 'structure',
    icon: Table2,
    keywords: ['table', 'grid', '표', '테이블'],
    action: (editor, range) =>
      replaceSlashTrigger(editor, range, chain =>
        chain.insertTable({ rows: 3, cols: 2, withHeaderRow: true }),
      ),
  },
  {
    id: 'codeBlock',
    category: 'structure',
    icon: Code2,
    keywords: ['code', 'pre', 'fence', '코드', '코드 블록'],
    action: (editor, range) =>
      replaceSlashTrigger(editor, range, chain => chain.setCodeBlock()),
  },
  {
    id: 'divider',
    category: 'structure',
    icon: Minus,
    keywords: ['divider', 'separator', 'rule', 'horizontal', '구분선', '수평선'],
    action: (editor, range) =>
      replaceSlashTrigger(editor, range, chain => chain.setHorizontalRule()),
  },
] as const

export const MARKDOWN_SLASH_COMMAND_CATEGORY_ORDER: readonly MarkdownSlashCommandCategory[] = [
  'text',
  'lists',
  'structure',
]

export function createMarkdownSlashCommandRegistry(
  messages: MarkdownSlashCommandMessages,
): readonly LocalizedMarkdownSlashCommand[] {
  return MARKDOWN_SLASH_COMMAND_DEFINITIONS.map(definition => ({
    ...definition,
    label: messages.commands[definition.id].label,
    description: messages.commands[definition.id].description,
  }))
}

export function filterMarkdownSlashCommands(
  commands: readonly LocalizedMarkdownSlashCommand[],
  query: string,
): LocalizedMarkdownSlashCommand[] {
  const normalized = query.trim().toLocaleLowerCase()
  if (!normalized) return [...commands]

  return commands.filter(command => {
    const haystack = [command.label, command.description, ...command.keywords]
      .join(' ')
      .toLocaleLowerCase()
    return haystack.includes(normalized)
  })
}

interface MarkdownSlashCommandMenuProps {
  items: readonly LocalizedMarkdownSlashCommand[]
  selectedIndex: number
  listboxId: string
  messages: MarkdownSlashCommandMessages
  onActiveChange: (index: number) => void
  onSelect: (command: LocalizedMarkdownSlashCommand) => void
}

function MarkdownSlashCommandMenu({
  items,
  selectedIndex,
  listboxId,
  messages,
  onActiveChange,
  onSelect,
}: MarkdownSlashCommandMenuProps) {
  const optionsRef = useRef<HTMLDivElement>(null)
  const selectedId = items[selectedIndex]?.id

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
      aria-label={messages.header}
      data-testid="slash-command-menu"
      className="markdown-slash-command-menu"
    >
      <div className="markdown-slash-command-header">
        <span className="markdown-slash-command-header-label">{messages.header}</span>
        <kbd className="markdown-slash-command-escape">{messages.escapeHint}</kbd>
      </div>

      <div ref={optionsRef} className="markdown-slash-command-options">
        {MARKDOWN_SLASH_COMMAND_CATEGORY_ORDER.map(category => {
          const categoryItems = items.filter(item => item.category === category)
          if (categoryItems.length === 0) return null

          return (
            <section
              key={category}
              aria-label={messages.sections[category]}
              data-slash-section={category}
              className="markdown-slash-command-section"
            >
              <div className="markdown-slash-command-section-label">
                {messages.sections[category]}
              </div>
              {categoryItems.map(item => {
                const itemIndex = items.findIndex(candidate => candidate.id === item.id)
                const Icon = item.icon
                return (
                  <button
                    key={item.id}
                    id={`${listboxId}-${item.id}`}
                    type="button"
                    role="option"
                    tabIndex={-1}
                    aria-selected={selectedId === item.id}
                    aria-label={`${item.label}: ${item.description}`}
                    data-slash-command={item.id}
                    className={[
                      'markdown-slash-command-option',
                      selectedId === item.id ? 'markdown-slash-command-option-selected' : '',
                    ].filter(Boolean).join(' ')}
                    onMouseDown={event => event.preventDefault()}
                    onPointerMove={() => onActiveChange(itemIndex)}
                    onClick={() => onSelect(items[itemIndex] ?? item)}
                  >
                    <Icon className="markdown-slash-command-icon" aria-hidden="true" />
                    <span className="markdown-slash-command-copy">
                      <span className="markdown-slash-command-label">{item.label}</span>
                      <span className="markdown-slash-command-description">
                        {item.description}
                      </span>
                    </span>
                  </button>
                )
              })}
            </section>
          )
        })}

        {items.length === 0 && (
          <div role="status" data-testid="slash-command-empty" className="markdown-slash-command-empty">
            {messages.empty}
          </div>
        )}
      </div>

      <div className="markdown-slash-command-footer" aria-hidden="true">
        <span>{messages.footer.navigation}</span>
        <span>{messages.footer.insert}</span>
        <span>{messages.footer.close}</span>
      </div>
    </div>
  )
}

function setMarkdownSlashAria(
  editor: Editor,
  listboxId: string,
  selectedId: MarkdownSlashCommandId | undefined,
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
  if (itemCount === 0) {
    element.removeAttribute('aria-activedescendant')
  } else if (selectedId) {
    element.setAttribute('aria-activedescendant', `${listboxId}-${selectedId}`)
  }
}

const MARKDOWN_SLASH_MENU_VIEWPORT_INSET = 8
const MARKDOWN_SLASH_MENU_OFFSET = 4
const MARKDOWN_SLASH_MENU_NOMINAL_WIDTH = 360
const MARKDOWN_SLASH_MENU_DEFAULT_HEIGHT = 280
const MARKDOWN_SLASH_MENU_MAX_HEIGHT = 420
const MARKDOWN_SLASH_MENU_OPTIONS_MAX_HEIGHT = 320

export interface MarkdownSlashMenuBoundary {
  left: number
  right: number
  top: number
  bottom: number
}

export interface ResolvedMarkdownSlashMenuPosition {
  visible: boolean
  left: number
  top: number
}

function isClippingOverflow(value: string): boolean {
  return ['auto', 'clip', 'hidden', 'overlay', 'scroll'].includes(value)
}

function hasPositiveArea(rect: Pick<DOMRect, 'left' | 'right' | 'top' | 'bottom'>): boolean {
  return rect.right > rect.left && rect.bottom > rect.top
}

function hasAnchorArea(rect: Pick<DOMRect, 'left' | 'right' | 'top' | 'bottom'>): boolean {
  return rect.right > rect.left || rect.bottom > rect.top
}

function intersectMarkdownSlashBoundary(
  boundary: MarkdownSlashMenuBoundary,
  rect: Pick<DOMRect, 'left' | 'right' | 'top' | 'bottom'>,
): void {
  if (!hasPositiveArea(rect)) return
  boundary.left = Math.max(boundary.left, rect.left + MARKDOWN_SLASH_MENU_VIEWPORT_INSET)
  boundary.right = Math.min(boundary.right, rect.right - MARKDOWN_SLASH_MENU_VIEWPORT_INSET)
  boundary.top = Math.max(boundary.top, rect.top + MARKDOWN_SLASH_MENU_VIEWPORT_INSET)
  boundary.bottom = Math.min(boundary.bottom, rect.bottom - MARKDOWN_SLASH_MENU_VIEWPORT_INSET)
}

/** Intersect the viewport with every clipping ancestor around the editor. */
export function getMarkdownSlashMenuBoundary(editorRoot: HTMLElement): MarkdownSlashMenuBoundary {
  const boundary: MarkdownSlashMenuBoundary = {
    left: MARKDOWN_SLASH_MENU_VIEWPORT_INSET,
    right: Math.max(
      MARKDOWN_SLASH_MENU_VIEWPORT_INSET,
      window.innerWidth - MARKDOWN_SLASH_MENU_VIEWPORT_INSET,
    ),
    top: MARKDOWN_SLASH_MENU_VIEWPORT_INSET,
    bottom: Math.max(
      MARKDOWN_SLASH_MENU_VIEWPORT_INSET,
      window.innerHeight - MARKDOWN_SLASH_MENU_VIEWPORT_INSET,
    ),
  }

  let current: HTMLElement | null = editorRoot
  while (current && current !== document.body) {
    const styles = getComputedStyle(current)
    if (isClippingOverflow(styles.overflowX) || isClippingOverflow(styles.overflowY)) {
      intersectMarkdownSlashBoundary(boundary, current.getBoundingClientRect())
    }
    current = current.parentElement
  }

  return boundary
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max)
}

/** Resolve below/above placement and clamp to the editor's clipping boundary. */
export function resolveMarkdownSlashMenuPosition({
  anchor,
  boundary,
  menuWidth,
  menuHeight,
  data,
}: {
  anchor: DOMRect | null
  boundary: MarkdownSlashMenuBoundary
  menuWidth: number
  menuHeight: number
  data: SuggestionPositionData
}): ResolvedMarkdownSlashMenuPosition {
  const width = Math.max(0, menuWidth)
  const height = Math.max(0, menuHeight)
  const scrollX = data.strategy === 'absolute' ? window.scrollX : 0
  const scrollY = data.strategy === 'absolute' ? window.scrollY : 0
  const boundaryLeft = boundary.left + scrollX
  const boundaryRight = boundary.right + scrollX
  const boundaryTop = boundary.top + scrollY
  const boundaryBottom = boundary.bottom + scrollY
  const maxLeft = Math.max(boundaryLeft, boundaryRight - width)
  const maxTop = Math.max(boundaryTop, boundaryBottom - height)
  const left = clamp(data.x, boundaryLeft, maxLeft)

  if (!anchor) return { visible: false, left, top: clamp(data.y, boundaryTop, maxTop) }
  if (!hasAnchorArea(anchor)) {
    return { visible: true, left, top: clamp(data.y, boundaryTop, maxTop) }
  }

  const anchorVisible =
    anchor.right >= boundary.left &&
    anchor.left <= boundary.right &&
    anchor.bottom > boundary.top &&
    anchor.top < boundary.bottom
  if (!anchorVisible) {
    return { visible: false, left, top: clamp(data.y, boundaryTop, maxTop) }
  }

  const anchorTop = anchor.top + scrollY
  const anchorBottom = anchor.bottom + scrollY
  const belowTop = anchorBottom + MARKDOWN_SLASH_MENU_OFFSET
  const aboveTop = anchorTop - height - MARKDOWN_SLASH_MENU_OFFSET
  const belowFits = belowTop + height <= boundaryBottom
  const aboveFits = aboveTop >= boundaryTop
  const belowSpace = Math.max(0, boundaryBottom - belowTop)
  const aboveSpace = Math.max(0, anchorTop - boundaryTop - MARKDOWN_SLASH_MENU_OFFSET)

  let top: number
  if (belowFits) top = belowTop
  else if (aboveFits) top = aboveTop
  else {
    top = belowSpace >= aboveSpace ? belowTop : aboveTop
    top = clamp(top, boundaryTop, maxTop)
  }

  return { visible: true, left, top }
}

export function ensureMarkdownSlashOptionVisible(
  listboxId: string,
  selectedId: MarkdownSlashCommandId | undefined,
): void {
  if (!selectedId) return
  const option = document.getElementById(`${listboxId}-${selectedId}`)
  const options = option?.closest<HTMLElement>('.markdown-slash-command-options')
  if (!option || !options) return

  const optionTop = option.offsetTop
  const optionBottom = optionTop + option.offsetHeight
  if (optionTop < options.scrollTop) options.scrollTop = optionTop
  else if (optionBottom > options.scrollTop + options.clientHeight) {
    options.scrollTop = optionBottom - options.clientHeight
  }
}

function getMarkdownSlashAnchorRect(
  editor: Editor,
  fallback: (() => DOMRect | null) | null | undefined,
): DOMRect | null {
  const decoration = editor.view.dom.querySelector<HTMLElement>(
    '.markdown-slash-command-suggestion[data-decoration-id]',
  )
  const decorationRect = decoration?.getBoundingClientRect()
  if (decorationRect && hasAnchorArea(decorationRect)) return decorationRect
  return fallback?.() ?? null
}

function positionMarkdownSlashMenu(
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
  const belowSpace = anchor
    ? Math.max(0, boundary.bottom - anchor.bottom - MARKDOWN_SLASH_MENU_OFFSET)
    : availableHeight
  const aboveSpace = anchor
    ? Math.max(0, anchor.top - boundary.top - MARKDOWN_SLASH_MENU_OFFSET)
    : availableHeight
  const sideHeight = anchor && hasAnchorArea(anchor)
    ? Math.max(belowSpace, aboveSpace)
    : availableHeight
  const maxMenuHeight = Math.min(
    MARKDOWN_SLASH_MENU_MAX_HEIGHT,
    availableHeight || MARKDOWN_SLASH_MENU_MAX_HEIGHT,
    sideHeight || availableHeight || MARKDOWN_SLASH_MENU_MAX_HEIGHT,
  )
  const nominalWidth =
    measuredRect.width ||
    Number.parseFloat(getComputedStyle(menu).width) ||
    MARKDOWN_SLASH_MENU_NOMINAL_WIDTH
  const width = Math.min(nominalWidth, availableWidth || nominalWidth)
  if (availableWidth > 0) menu.style.width = `${width}px`

  if (availableHeight > 0) {
    menu.style.maxHeight = `${maxMenuHeight}px`
    const options = menu.querySelector<HTMLElement>('.markdown-slash-command-options')
    if (options) {
      const headerHeight =
        menu.querySelector<HTMLElement>('.markdown-slash-command-header')?.getBoundingClientRect()
          .height || 36
      const footerHeight =
        menu.querySelector<HTMLElement>('.markdown-slash-command-footer')?.getBoundingClientRect()
          .height || 30
      options.style.maxHeight = `${Math.min(
        MARKDOWN_SLASH_MENU_OPTIONS_MAX_HEIGHT,
        Math.max(0, maxMenuHeight - headerHeight - footerHeight),
      )}px`
    }
  }

  const boundedRect = menu.getBoundingClientRect()
  const height = Math.min(
    boundedRect.height || measuredRect.height || MARKDOWN_SLASH_MENU_DEFAULT_HEIGHT,
    availableHeight || MARKDOWN_SLASH_MENU_MAX_HEIGHT,
  )
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

function createMarkdownSlashSuggestion(
  options: MarkdownSlashCommandOptions,
): Omit<
  SuggestionOptions<LocalizedMarkdownSlashCommand, LocalizedMarkdownSlashCommand>,
  'editor'
> {
  const messages = options.messages ?? DEFAULT_MARKDOWN_SLASH_COMMAND_MESSAGES
  const commands = createMarkdownSlashCommandRegistry(messages)
  let renderer: ReactRenderer<unknown, MarkdownSlashCommandMenuProps> | null = null
  let unmount: (() => void) | undefined
  let selectedIndex = 0
  let items: LocalizedMarkdownSlashCommand[] = []
  let command: ((item: LocalizedMarkdownSlashCommand) => void) | undefined
  let activeEditor: Editor | null = null
  const listboxId = `markdown-slash-command-list-${Math.random().toString(36).slice(2, 10)}`

  function updateRenderer(
    props: SuggestionProps<LocalizedMarkdownSlashCommand, LocalizedMarkdownSlashCommand>,
  ): void {
    items = props.items
    selectedIndex = 0
    command = props.command
    setMarkdownSlashAria(
      props.editor,
      listboxId,
      items[selectedIndex]?.id,
      items.length,
      true,
    )
    renderer?.updateProps({
      items,
      selectedIndex,
      listboxId,
      messages,
      onActiveChange: setActiveIndex,
      onSelect: (item: LocalizedMarkdownSlashCommand) => command?.(item),
    })
    const optionsElement = renderer?.element.querySelector<HTMLElement>(
      '.markdown-slash-command-options',
    )
    if (optionsElement) optionsElement.scrollTop = 0
    queueMicrotask(() =>
      ensureMarkdownSlashOptionVisible(listboxId, items[selectedIndex]?.id),
    )
  }

  function setActiveIndex(nextIndex: number): void {
    if (nextIndex < 0 || nextIndex >= items.length) return
    selectedIndex = nextIndex
    renderer?.updateProps({ selectedIndex })
    queueMicrotask(() =>
      ensureMarkdownSlashOptionVisible(listboxId, items[selectedIndex]?.id),
    )
    if (activeEditor) {
      setMarkdownSlashAria(
        activeEditor,
        listboxId,
        items[selectedIndex]?.id,
        items.length,
        true,
      )
    }
  }

  return {
    pluginKey: markdownSlashCommandPluginKey,
    char: '/',
    allowSpaces: true,
    allowedPrefixes: null,
    startOfLine: true,
    placement: 'bottom-start',
    flip: true,
    decorationClass: 'markdown-slash-command-suggestion',
    allow: ({ editor }) => {
      if (!editor.isEditable || editor.state.selection.$from.parent.type.name !== 'paragraph') {
        return false
      }
      return !editor.view.composing && !editor.isActive('code')
    },
    findSuggestionMatch: config => {
      const match = findSuggestionMatch({
        ...config,
        allowedPrefixes: null,
        startOfLine: true,
      })
      if (!match || config.$position.parent.type.name !== 'paragraph') return null
      const paragraphStart = config.$position.start()
      const before = config.$position.doc.textBetween(
        paragraphStart,
        match.range.from,
        '\n',
      )
      return before.length === 0 ? match : null
    },
    items: ({ query }: { query: string }) => filterMarkdownSlashCommands(commands, query),
    command: ({
      editor,
      range,
      props,
    }: {
      editor: Editor
      range: Range
      props: LocalizedMarkdownSlashCommand
    }) => props.action(editor, range),
    render: () => ({
      onStart: (
        props: SuggestionProps<LocalizedMarkdownSlashCommand, LocalizedMarkdownSlashCommand>,
      ) => {
        options.onOpenChange?.(true, () =>
          exitSuggestion(props.editor.view, markdownSlashCommandPluginKey),
        )
        activeEditor = props.editor
        selectedIndex = 0
        items = props.items
        command = props.command
        renderer = new ReactRenderer(MarkdownSlashCommandMenu, {
          editor: props.editor,
          className: 'markdown-slash-command-popup',
          props: {
            items,
            selectedIndex,
            listboxId,
            messages,
            onActiveChange: setActiveIndex,
            onSelect: (item: LocalizedMarkdownSlashCommand) => command?.(item),
          },
        })
        renderer.element.dataset.testid = 'slash-command-popup'
        unmount = props.mount(renderer.element, {
          onPosition: data => {
            const positioned = positionMarkdownSlashMenu(
              renderer?.element ?? document.body,
              props.editor,
              data,
              getMarkdownSlashAnchorRect(props.editor, props.clientRect),
            )
            if (!positioned) exitSuggestion(props.editor.view, markdownSlashCommandPluginKey)
          },
        })
        setMarkdownSlashAria(
          props.editor,
          listboxId,
          items[selectedIndex]?.id,
          items.length,
          true,
        )
      },
      onUpdate: updateRenderer,
      onExit: ({
        editor,
      }: SuggestionProps<LocalizedMarkdownSlashCommand, LocalizedMarkdownSlashCommand>) => {
        options.onOpenChange?.(false)
        setMarkdownSlashAria(editor, listboxId, undefined, items.length, false)
        unmount?.()
        unmount = undefined
        renderer?.destroy()
        renderer = null
        items = []
        command = undefined
        selectedIndex = 0
        activeEditor = null
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
          exitSuggestion(view, markdownSlashCommandPluginKey)
          return true
        }
        return false
      },
    }),
  }
}

export function createMarkdownSlashCommandExtension(
  options: MarkdownSlashCommandOptions = {},
): Extension {
  const suggestion = createMarkdownSlashSuggestion(options)
  return Extension.create({
    name: 'markdownSlashCommand',
    addProseMirrorPlugins() {
      return [Suggestion({ ...suggestion, editor: this.editor })]
    },
  })
}
