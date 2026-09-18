import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Editor } from '@tiptap/core'
import { EditorContent } from '@tiptap/react'
import { afterEach, describe, expect, it } from 'vitest'

import { createMarkdownExtensions } from '../src/extensions.js'
import {
  createMarkdownSlashCommandExtension,
  createMarkdownSlashCommandRegistry,
  DEFAULT_MARKDOWN_SLASH_COMMAND_MESSAGES,
  filterMarkdownSlashCommands,
  getMarkdownSlashMenuBoundary,
  resolveMarkdownSlashMenuPosition,
} from '../src/react/markdown-slash-command.js'

const editors: Editor[] = []

function dispatchKey(editor: Editor, key: string): boolean {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
  return editor.view.someProp('handleKeyDown', handler => handler(editor.view, event)) ?? false
}

function mountEditor(markdown = ''): Editor {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const editor = new Editor({
    element: host,
    extensions: [
      ...createMarkdownExtensions(),
      createMarkdownSlashCommandExtension(),
    ],
    content: markdown,
    contentType: 'markdown',
  })
  editors.push(editor)
  render(<EditorContent editor={editor} />)
  return editor
}

afterEach(() => {
  while (editors.length > 0) editors.pop()?.destroy()
  document.body.innerHTML = ''
})

describe('markdown slash command menu', () => {
  it('defines all common blocks and filters translated labels and keywords', () => {
    expect(DEFAULT_MARKDOWN_SLASH_COMMAND_MESSAGES.sections).toEqual({
      text: 'Text',
      lists: 'Lists',
      structure: 'Structure',
    })
    const commands = createMarkdownSlashCommandRegistry(DEFAULT_MARKDOWN_SLASH_COMMAND_MESSAGES)

    expect(commands).toHaveLength(10)
    expect(filterMarkdownSlashCommands(commands, 'table').map(item => item.id)).toEqual(['table'])
    expect(filterMarkdownSlashCommands(commands, '표').map(item => item.id)).toEqual(['table'])
    expect(filterMarkdownSlashCommands(commands, 'checklist').map(item => item.id)).toEqual([
      'taskList',
    ])
  })

  it('opens a categorized ARIA listbox at paragraph start without an input', async () => {
    const editor = mountEditor()
    await act(async () => editor.commands.insertContent('/'))

    const menu = await screen.findByTestId('slash-command-menu')
    expect(menu.querySelectorAll('[role="option"]')).toHaveLength(10)
    expect(menu.querySelectorAll('[data-slash-section]')).toHaveLength(3)
    expect(menu.querySelector('input')).toBeNull()
    expect(editor.view.dom).toHaveAttribute('aria-autocomplete', 'list')
    expect(editor.view.dom).toHaveAttribute('aria-expanded', 'true')
    expect(editor.view.dom).toHaveAttribute('aria-controls', menu.id)
    expect(editor.view.dom).toHaveAttribute(
      'aria-activedescendant',
      menu.querySelector('[role="option"]')?.id,
    )

    await act(async () => editor.commands.insertContent('표'))
    await waitFor(() => expect(menu.querySelectorAll('[role="option"]')).toHaveLength(1))
    expect(menu.querySelector('[data-slash-command="table"]')).toBeInTheDocument()
    expect(menu.querySelectorAll('[data-slash-section]')).toHaveLength(1)
  })

  it('rejects inline text, code blocks, read-only state, and composition', async () => {
    const inline = mountEditor('inline /')
    expect(document.querySelector('[data-testid="slash-command-menu"]')).toBeNull()

    const code = mountEditor('```\n')
    await act(async () => code.commands.setCodeBlock())
    await act(async () => code.commands.insertContent('/'))
    expect(document.querySelector('[data-testid="slash-command-menu"]')).toBeNull()

    const readOnly = mountEditor()
    readOnly.setEditable(false)
    await act(async () => readOnly.commands.insertContent('/'))
    expect(document.querySelector('[data-testid="slash-command-menu"]')).toBeNull()

    const composing = mountEditor()
    Object.defineProperty(composing.view, 'composing', {
      configurable: true,
      value: true,
    })
    await act(async () => composing.commands.insertContent('/'))
    expect(document.querySelector('[data-testid="slash-command-menu"]')).toBeNull()
    Object.defineProperty(composing.view, 'composing', {
      configurable: true,
      value: false,
    })

    inline.destroy()
  })

  it('consumes Escape without bubbling and keeps the slash query', async () => {
    const editor = mountEditor()
    const host = editor.view.dom.parentElement!
    let bubbled = false
    host.addEventListener('keydown', () => {
      bubbled = true
    })
    await act(async () => editor.commands.insertContent('/'))
    await screen.findByTestId('slash-command-menu')

    fireEvent.keyDown(editor.view.dom, { key: 'Escape' })
    await waitFor(() =>
      expect(document.querySelector('[data-testid="slash-command-menu"]')).toBeNull(),
    )
    expect(bubbled).toBe(false)
    expect(editor.getMarkdown()).toContain('/')
    expect(editor.view.dom).toHaveAttribute('aria-expanded', 'false')
  })

  it('leaves an empty result untouched when Enter is pressed', async () => {
    const editor = mountEditor()
    await act(async () => editor.commands.insertContent('/does-not-exist'))
    const menu = await screen.findByTestId('slash-command-menu')
    expect(menu.querySelectorAll('[role="option"]')).toHaveLength(0)

    dispatchKey(editor, 'Enter')
    expect(editor.getMarkdown()).toContain('/does-not-exist')
  })

  it('executes pointer and keyboard selections through the shared blocks', async () => {
    const pointerEditor = mountEditor()
    await act(async () => pointerEditor.commands.insertContent('/ta'))
    const tableMenu = await screen.findByTestId('slash-command-menu')
    const table = tableMenu.querySelector<HTMLButtonElement>('[data-slash-command="table"]')!
    fireEvent.mouseDown(table)
    fireEvent.click(table)

    await waitFor(() =>
      expect(document.querySelector('[data-testid="slash-command-menu"]')).toBeNull(),
    )
    expect(pointerEditor.view.dom.querySelectorAll('table tr')).toHaveLength(3)
    expect(pointerEditor.getMarkdown()).not.toContain('/')

    const keyboardEditor = mountEditor()
    await act(async () => keyboardEditor.commands.insertContent('/'))
    const keyboardMenu = await screen.findByTestId('slash-command-menu')
    expect(keyboardMenu.querySelector('[data-slash-command="heading1"]')).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await act(async () => dispatchKey(keyboardEditor, 'ArrowUp'))
    expect(keyboardMenu.querySelector('[data-slash-command="divider"]')).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await act(async () => dispatchKey(keyboardEditor, 'ArrowDown'))
    expect(keyboardMenu.querySelector('[data-slash-command="heading1"]')).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await act(async () => dispatchKey(keyboardEditor, 'Enter'))
    await waitFor(() => expect(keyboardEditor.view.dom.querySelector('h1')).toBeInTheDocument())
    expect(keyboardEditor.getMarkdown()).not.toContain('/')

    const taskEditor = mountEditor()
    await act(async () => taskEditor.commands.insertContent('/check'))
    const taskMenu = await screen.findByTestId('slash-command-menu')
    fireEvent.click(taskMenu.querySelector('[data-slash-command="taskList"]')!)
    await waitFor(() => expect(taskEditor.view.dom.querySelector('[data-type="taskList"]')).toBeInTheDocument())
  })

  it('clamps, flips, and hides the menu using viewport and clipping boundaries', () => {
    const boundary = { left: 8, right: 492, top: 8, bottom: 392 }
    const data = { x: 20, y: 370, placement: 'bottom-start' as const, strategy: 'fixed' as const }
    const anchor = new DOMRect(20, 350, 2, 10)
    const flipped = resolveMarkdownSlashMenuPosition({
      anchor,
      boundary,
      menuWidth: 180,
      menuHeight: 120,
      data,
    })
    expect(flipped.visible).toBe(true)
    expect(flipped.top).toBe(226)

    const outside = resolveMarkdownSlashMenuPosition({
      anchor: new DOMRect(20, 500, 2, 10),
      boundary,
      menuWidth: 180,
      menuHeight: 120,
      data,
    })
    expect(outside.visible).toBe(false)

    const clipping = document.createElement('div')
    const root = document.createElement('div')
    clipping.style.overflowX = 'auto'
    clipping.style.overflowY = 'auto'
    clipping.append(root)
    document.body.append(clipping)
    Object.defineProperty(clipping, 'getBoundingClientRect', {
      configurable: true,
      value: () => new DOMRect(20, 30, 300, 240),
    })
    Object.defineProperty(root, 'getBoundingClientRect', {
      configurable: true,
      value: () => new DOMRect(30, 40, 200, 120),
    })
    const clipped = getMarkdownSlashMenuBoundary(root)
    expect(clipped).toMatchObject({ left: 28, right: 312, top: 38, bottom: 262 })
  })
})
