import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import type { Editor } from '@tiptap/core'
import { Columns2, Columns3, CornerDownLeft, Rows2, Rows3, Table, Trash2 } from 'lucide-react'

import { markdownCommands } from '../core.js'
import {
  isSelectedMarkdownTable,
  markdownTableState,
} from '../table.js'
import type { MarkdownTableState } from '../table.js'
import { useMarkdownMessages } from './markdown-locale.js'

export interface MarkdownTableOptions {
  tableClassName?: string
  captionClassName?: string
}

const DEFAULT_TABLE_CLASS_NAME = 'w-full min-w-[36rem] border-collapse border border-border text-sm'
const DEFAULT_CAPTION_CLASS_NAME = 'caption-top border-b border-border bg-surface-2 px-2 py-1.5 text-left'
const ACTION_BUTTON_CLASS_NAME =
  'inline-flex min-h-8 items-center justify-center gap-1.5 rounded-[var(--radius-sm)] px-2 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50'

interface TableHost {
  table: HTMLTableElement
  host: HTMLDivElement
}

function tableClasses(table: HTMLTableElement, className: string): void {
  const classes = className.split(/\s+/).filter(Boolean)
  for (const name of classes) table.classList.add(name)
}

function useTableHosts(
  editor: Editor | null,
  readOnly: boolean,
  options: MarkdownTableOptions | undefined,
  labels: ReturnType<typeof useMarkdownMessages>['table'],
): TableHost[] {
  const [hosts, setHosts] = useState<TableHost[]>([])
  const hostsRef = useRef<TableHost[]>([])
  const labelsRef = useRef(labels)
  const syncRef = useRef<(() => void) | undefined>(undefined)
  const tableClassName = [DEFAULT_TABLE_CLASS_NAME, options?.tableClassName].filter(Boolean).join(' ')
  const captionClassName = [DEFAULT_CAPTION_CLASS_NAME, options?.captionClassName].filter(Boolean).join(' ')

  useLayoutEffect(() => {
    const root = editor?.view.dom
    if (!root || !editor) return

    const sync = () => {
      const next: TableHost[] = []
      root.querySelectorAll<HTMLTableElement>('table').forEach(table => {
        tableClasses(table, tableClassName)
        table.setAttribute(
          'aria-label',
          readOnly ? labelsRef.current.readOnlyTable : labelsRef.current.editableTable,
        )

        const existingCaption = Array.from(table.children).find(
          child => child instanceof HTMLTableCaptionElement && child.dataset.markdownTableCaption === 'true',
        ) as HTMLTableCaptionElement | undefined

        if (readOnly) {
          existingCaption?.remove()
          return
        }

        const caption = existingCaption ?? window.document.createElement('caption')
        caption.dataset.markdownTableCaption = 'true'
        caption.contentEditable = 'false'
        caption.className = captionClassName
        if (!existingCaption) table.prepend(caption)

        let host = Array.from(caption.children).find(
          child => child instanceof HTMLDivElement && child.dataset.markdownTableControls === 'true',
        ) as HTMLDivElement | undefined
        if (!host) {
          host = window.document.createElement('div')
          host.dataset.markdownTableControls = 'true'
          caption.append(host)
        }
        next.push({ table, host })
      })

      const nextHosts = new Set(next.map(entry => entry.host))
      for (const current of hostsRef.current) {
        if (!nextHosts.has(current.host)) current.host.remove()
      }
      const unchanged =
        next.length === hostsRef.current.length &&
        next.every((entry, index) => entry.table === hostsRef.current[index]?.table)
      hostsRef.current = next
      if (!unchanged) setHosts(next)
    }
    syncRef.current = sync

    sync()
    editor.on('transaction', sync)
    const observer = typeof MutationObserver === 'undefined' ? null : new MutationObserver(sync)
    observer?.observe(root, { childList: true, subtree: true })

    return () => {
      editor.off('transaction', sync)
      observer?.disconnect()
      for (const current of hostsRef.current) current.host.remove()
      hostsRef.current = []
      syncRef.current = undefined
    }
  }, [captionClassName, editor, readOnly, tableClassName])

  useLayoutEffect(() => {
    labelsRef.current = labels
    syncRef.current?.()
  }, [labels])

  return hosts
}

interface TableActionButtonProps {
  label: string
  disabled: boolean
  onClick: () => void
  children: ReactNode
  className?: string
  tabIndex: number
  onFocus: () => void
}

function TableActionButton({
  label,
  disabled,
  onClick,
  children,
  className,
  tabIndex,
  onFocus,
}: TableActionButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      tabIndex={tabIndex}
      onMouseDown={event => event.preventDefault()}
      onFocus={onFocus}
      onClick={onClick}
      className={`${ACTION_BUTTON_CLASS_NAME} ${className ?? ''}`.trim()}
    >
      {children}
      <span>{label}</span>
    </button>
  )
}

function TableActionToolbar({
  editor,
  labels,
  state,
  active,
}: {
  editor: Editor
  labels: ReturnType<typeof useMarkdownMessages>['table']
  state: MarkdownTableState
  active: boolean
}) {
  const commands = useMemo(() => markdownCommands(editor), [editor])
  const [focusedIndex, setFocusedIndex] = useState(0)
  const toolbarRef = useRef<HTMLDivElement>(null)

  const controls = useMemo(() => [
    {
      key: 'add-row',
      label: labels.addRow,
      disabled: !active || !state.canAddRowAfter,
      onClick: commands.addTableRowAfter,
      icon: <Rows3 className="h-3.5 w-3.5" aria-hidden />,
    },
    {
      key: 'add-column',
      label: labels.addColumn,
      disabled: !active || !state.canAddColumnAfter,
      onClick: commands.addTableColumnAfter,
      icon: <Columns3 className="h-3.5 w-3.5" aria-hidden />,
    },
    {
      key: 'remove-row',
      label: labels.removeRow,
      disabled: !active || !state.canDeleteRow,
      onClick: commands.deleteTableRow,
      icon: <Rows2 className="h-3.5 w-3.5" aria-hidden />,
    },
    {
      key: 'remove-column',
      label: labels.removeColumn,
      disabled: !active || !state.canDeleteColumn,
      onClick: commands.deleteTableColumn,
      icon: <Columns2 className="h-3.5 w-3.5" aria-hidden />,
    },
    {
      key: 'continue-below',
      label: labels.continueBelow,
      disabled: !active || !state.canContinueBelow,
      onClick: commands.continueBelowTable,
      icon: <CornerDownLeft className="h-3.5 w-3.5" aria-hidden />,
    },
    {
      key: 'delete-table',
      label: labels.deleteTable,
      disabled: !active || !state.canDelete,
      onClick: commands.deleteTable,
      icon: <Trash2 className="h-3.5 w-3.5" aria-hidden />,
      className: 'text-foreground-muted hover:bg-destructive/10 hover:text-destructive',
    },
  ], [
    active,
    commands,
    labels.addColumn,
    labels.addRow,
    labels.continueBelow,
    labels.deleteTable,
    labels.removeColumn,
    labels.removeRow,
    state.canAddColumnAfter,
    state.canAddRowAfter,
    state.canContinueBelow,
    state.canDelete,
    state.canDeleteColumn,
    state.canDeleteRow,
  ])

  useLayoutEffect(() => {
    const buttons = Array.from(
      toolbarRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [],
    )
    if (buttons.length === 0 || buttons.some(button => button.tabIndex === 0)) return
    const label = buttons[0]?.getAttribute('aria-label')
    const index = controls.findIndex(control => control.label === label)
    if (index >= 0) setFocusedIndex(index)
  }, [controls])

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const target = event.target
    if (!(target instanceof HTMLButtonElement)) {
      return
    }

    if (event.key === 'Enter' || event.key === ' ') {
      const index = controls.findIndex(control => control.label === target.getAttribute('aria-label'))
      if (index < 0 || controls[index]?.disabled) return
      event.preventDefault()
      event.stopPropagation()
      controls[index]?.onClick()
      return
    }

    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    const buttons = Array.from(
      toolbarRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [],
    )
    if (buttons.length === 0) return
    event.preventDefault()
    const current = Math.max(0, buttons.indexOf(target))
    const next = event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? buttons.length - 1
        : (current + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length
    const nextLabel = buttons[next]?.getAttribute('aria-label')
    const nextIndex = controls.findIndex(control => control.label === nextLabel)
    if (nextIndex >= 0) setFocusedIndex(nextIndex)
    buttons[next]?.focus()
  }

  return (
    <div className="flex min-w-max items-center justify-between gap-3">
      <span className="inline-flex items-center gap-1.5 px-1 text-xs font-medium text-foreground-muted">
        <Table className="h-3.5 w-3.5" aria-hidden />
        {labels.editableTable}
      </span>
      <div
        ref={toolbarRef}
        className="flex items-center gap-1"
        role="toolbar"
        aria-label={labels.actions}
        aria-orientation="horizontal"
        onFocusCapture={event => {
          const target = event.target
          if (!(target instanceof HTMLButtonElement)) return
          const enabled = Array.from(
            toolbarRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [],
          )
          setFocusedIndex(Math.max(0, enabled.indexOf(target)))
        }}
        onKeyDown={handleKeyDown}
      >
        {controls.map((control, index) => (
          <TableActionButton
            key={control.key}
            label={control.label}
            disabled={control.disabled}
            onClick={control.onClick}
            className={control.className}
            tabIndex={index === focusedIndex ? 0 : -1}
            onFocus={() => setFocusedIndex(index)}
          >
            {control.icon}
          </TableActionButton>
        ))}
      </div>
    </div>
  )
}

export function MarkdownTableControls({
  editor,
  readOnly,
  options,
}: {
  editor: Editor | null
  readOnly: boolean
  options?: MarkdownTableOptions
}) {
  const [, setRevision] = useState(0)
  const labels = useMarkdownMessages().table
  const hosts = useTableHosts(editor, readOnly, options, labels)

  useEffect(() => {
    if (!editor) return
    const refresh = () => setRevision(value => value + 1)
    editor.on('transaction', refresh)
    editor.on('selectionUpdate', refresh)
    return () => {
      editor.off('transaction', refresh)
      editor.off('selectionUpdate', refresh)
    }
  }, [editor])

  if (readOnly || !editor) return null
  const state = markdownTableState(editor)
  return <>{hosts.map((host, index) => createPortal(
    <TableActionToolbar
      key={index}
      editor={editor}
      labels={labels}
      state={state}
      active={state.active && isSelectedMarkdownTable(editor, host.table)}
    />,
    host.host,
  ))}</>
}
