import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { RefObject } from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import type { Editor } from '@tiptap/core'
import { Pencil, Replace, X } from 'lucide-react'

import { markdownCommands } from '../core.js'

export interface MarkdownImageMenuLabels {
  editDescription: (alt: string) => string
  replaceImage: (alt: string) => string
  removeImage: (alt: string) => string
  dialogTitle: string
  dialogDescription: string
  description: string
  cancel: string
  saveDescription: string
  descriptionRequired: string
  closeDialog: string
}

export interface MarkdownImageMenuClassNames {
  host?: string
  action?: string
  destructiveAction?: string
  dialog?: string
  field?: string
  error?: string
}

/**
 * Product copy, image target policy, styling, and replacement uploads stay at
 * the adapter boundary. The editor owns target selection and image mutations.
 */
export interface MarkdownImageMenuOptions {
  labels?: Partial<MarkdownImageMenuLabels>
  classNames?: MarkdownImageMenuClassNames
  isEditableTarget?: (target: string) => boolean
  onReplace?: (position: number) => void
}

const DEFAULT_LABELS: MarkdownImageMenuLabels = {
  editDescription: alt => alt ? `Edit image description: ${alt}` : 'Edit image description',
  replaceImage: alt => alt ? `Replace image: ${alt}` : 'Replace image',
  removeImage: alt => alt ? `Remove image: ${alt}` : 'Remove image',
  dialogTitle: 'Image description',
  dialogDescription: 'This text is used as the image alt text and visible caption.',
  description: 'Description',
  cancel: 'Cancel',
  saveDescription: 'Save description',
  descriptionRequired: 'Describe the image so it remains understandable without sight.',
  closeDialog: 'Close dialog',
}

const DEFAULT_HOST_CLASS_NAME =
  'absolute z-20 flex items-center gap-1 rounded-[var(--radius-md)] border border-border bg-surface/90 p-1 shadow-sm backdrop-blur-sm'
const DEFAULT_ACTION_CLASS_NAME =
  'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface'
const DEFAULT_DESTRUCTIVE_ACTION_CLASS_NAME =
  'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-destructive/10 hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface'
const DEFAULT_DIALOG_CLASS_NAME =
  'fixed left-1/2 top-1/2 z-[var(--z-modal)] grid w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 gap-4 overflow-y-auto rounded-[var(--radius-xl)] border border-border bg-surface p-6 text-foreground shadow-lg focus:outline-none'
const DEFAULT_FIELD_CLASS_NAME =
  'h-10 w-full rounded-[var(--radius-sm)] border border-border bg-surface px-3 py-2 text-sm text-foreground transition-token placeholder:text-foreground-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface'

interface ImageMenuEntry {
  id: number
  image: HTMLImageElement
  target: string
  alt: string
  top: number
  left: number
}

function imagePosition(editor: Editor, image: HTMLImageElement): number | null {
  if (editor.isDestroyed || !image.isConnected) return null
  try {
    const position = editor.view.posAtDOM(image, 0)
    return editor.state.doc.nodeAt(position)?.type.name === 'image' ? position : null
  } catch {
    return null
  }
}

function ImageMenuActions({
  editor,
  image,
  alt,
  options,
}: {
  editor: Editor
  image: HTMLImageElement
  alt: string
  options: MarkdownImageMenuOptions
}) {
  const labels = { ...DEFAULT_LABELS, ...options.labels }
  const classNames = options.classNames
  const commands = useMemo(() => markdownCommands(editor), [editor])
  const [descriptionOpen, setDescriptionOpen] = useState(false)
  const [description, setDescription] = useState(alt)
  const [descriptionError, setDescriptionError] = useState('')
  const descriptionId = useId()

  const saveDescription = () => {
    const next = description.trim()
    if (!next) {
      setDescriptionError(labels.descriptionRequired)
      return
    }
    const position = imagePosition(editor, image)
    if (position === null || !commands.setImageAltAt(position, next)) return
    setDescriptionError('')
    setDescriptionOpen(false)
  }

  const removeImage = () => {
    const position = imagePosition(editor, image)
    if (position !== null) commands.deleteImageAt(position)
  }

  const openDescription = () => {
    setDescription(alt)
    setDescriptionError('')
    setDescriptionOpen(true)
  }

  const actionClassName = [DEFAULT_ACTION_CLASS_NAME, classNames?.action].filter(Boolean).join(' ')
  const destructiveActionClassName = [
    DEFAULT_DESTRUCTIVE_ACTION_CLASS_NAME,
    classNames?.destructiveAction,
  ].filter(Boolean).join(' ')

  return (
    <>
      <button
        type="button"
        aria-label={labels.editDescription(alt)}
        aria-haspopup="dialog"
        aria-expanded={descriptionOpen}
        title={labels.editDescription(alt)}
        className={actionClassName}
        onMouseDown={event => event.preventDefault()}
        onClick={openDescription}
      >
        <Pencil className="h-3.5 w-3.5" aria-hidden />
      </button>
      {options.onReplace && (
        <button
          type="button"
          aria-label={labels.replaceImage(alt)}
          title={labels.replaceImage(alt)}
          className={actionClassName}
          onMouseDown={event => event.preventDefault()}
          onClick={() => {
            const position = imagePosition(editor, image)
            if (position !== null) options.onReplace?.(position)
          }}
        >
          <Replace className="h-3.5 w-3.5" aria-hidden />
        </button>
      )}
      <button
        type="button"
        aria-label={labels.removeImage(alt)}
        title={labels.removeImage(alt)}
        className={destructiveActionClassName}
        onMouseDown={event => event.preventDefault()}
        onClick={removeImage}
      >
        <X className="h-3.5 w-3.5" aria-hidden />
      </button>
      <DialogPrimitive.Root
        open={descriptionOpen}
        onOpenChange={open => {
          setDescriptionOpen(open)
          if (!open) setDescriptionError('')
        }}
      >
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="fixed inset-0 z-[var(--z-overlay)] bg-black/50 data-[state=open]:animate-in data-[state=closed]:animate-out" />
          <DialogPrimitive.Content
            className={[DEFAULT_DIALOG_CLASS_NAME, classNames?.dialog].filter(Boolean).join(' ')}
            onCloseAutoFocus={event => {
              event.preventDefault()
              if (!editor.isDestroyed) editor.commands.focus()
            }}
          >
            <DialogPrimitive.Title className="text-lg font-semibold tracking-tight">
              {labels.dialogTitle}
            </DialogPrimitive.Title>
            <DialogPrimitive.Description className="-mt-3 text-sm text-foreground-muted">
              {labels.dialogDescription}
            </DialogPrimitive.Description>
            <div className="space-y-2">
              <label htmlFor={descriptionId} className="text-sm font-medium">
                {labels.description}
              </label>
              <input
                id={descriptionId}
                type="text"
                autoFocus
                value={description}
                onChange={event => {
                  setDescription(event.currentTarget.value)
                  if (descriptionError) setDescriptionError('')
                }}
                aria-invalid={descriptionError ? true : undefined}
                aria-describedby={descriptionError ? `${descriptionId}-error` : undefined}
                className={[DEFAULT_FIELD_CLASS_NAME, classNames?.field].filter(Boolean).join(' ')}
              />
              {descriptionError && (
                <p
                  id={`${descriptionId}-error`}
                  role="alert"
                  className={['text-xs text-destructive', classNames?.error].filter(Boolean).join(' ')}
                >
                  {descriptionError}
                </p>
              )}
            </div>
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <button
                type="button"
                className="inline-flex min-h-9 items-center justify-center rounded-[var(--radius-sm)] border border-border bg-surface px-3 text-sm font-medium text-foreground transition-token hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                onClick={() => setDescriptionOpen(false)}
              >
                {labels.cancel}
              </button>
              <button
                type="button"
                className="inline-flex min-h-9 items-center justify-center rounded-[var(--radius-sm)] bg-primary px-3 text-sm font-medium text-primary-foreground transition-token hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                onClick={saveDescription}
              >
                {labels.saveDescription}
              </button>
            </div>
            <DialogPrimitive.Close
              type="button"
              aria-label={labels.closeDialog}
              className="absolute right-2 top-2 inline-flex h-9 w-9 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
            >
              <X className="h-4 w-4" aria-hidden />
            </DialogPrimitive.Close>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
    </>
  )
}

/** Attach one shared action menu to every eligible image node in this editor. */
export function MarkdownImageMenuControls({
  editor,
  rootRef,
  readOnly,
  options,
}: {
  editor: Editor | null
  rootRef: RefObject<HTMLDivElement | null>
  readOnly: boolean
  options?: MarkdownImageMenuOptions
}) {
  const [entries, setEntries] = useState<ImageMenuEntry[]>([])
  const entriesRef = useRef<ImageMenuEntry[]>([])
  const imageIds = useRef(new WeakMap<HTMLImageElement, number>())
  const nextImageId = useRef(0)
  const enabled = Boolean(options)
  const effectiveReadOnly = readOnly || Boolean(editor && !editor.isEditable)
  const isEditableTarget = options?.isEditableTarget
  const onReplace = options?.onReplace
  const hasReplace = Boolean(onReplace)

  useLayoutEffect(() => {
    const editorRoot = editor?.view.dom
    const surfaceRoot = rootRef.current
    if (!editor || !editorRoot || !surfaceRoot || effectiveReadOnly || !enabled) {
      entriesRef.current = []
      setEntries([])
      return
    }

    const sync = () => {
      const rootRect = surfaceRoot.getBoundingClientRect()
      const next: ImageMenuEntry[] = []
      editorRoot.querySelectorAll<HTMLImageElement>('img[data-markdown-target]').forEach(image => {
        const target = image.dataset.markdownTarget ?? ''
        if (!target || (isEditableTarget && !isEditableTarget(target))) return

        let id = imageIds.current.get(image)
        if (id === undefined) {
          id = ++nextImageId.current
          imageIds.current.set(image, id)
        }
        const rect = image.getBoundingClientRect()
        next.push({
          id,
          image,
          target,
          alt: image.getAttribute('alt') ?? '',
          top: Math.max(0, rect.top - rootRect.top + 4),
          left: Math.max(0, rect.right - rootRect.left - (hasReplace ? 112 : 76)),
        })
      })

      const unchanged =
        next.length === entriesRef.current.length &&
        next.every((entry, index) => {
          const current = entriesRef.current[index]
          return current?.image === entry.image &&
            current.alt === entry.alt &&
            current.target === entry.target &&
            current.top === entry.top &&
            current.left === entry.left
        })
      entriesRef.current = next
      if (!unchanged) setEntries(next)
    }

    const resizeObserver = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(sync)
    resizeObserver?.observe(surfaceRoot)
    resizeObserver?.observe(editorRoot)
    const observeImages = () => {
      editorRoot.querySelectorAll<HTMLImageElement>('img[data-markdown-target]').forEach(image => {
        resizeObserver?.observe(image)
      })
    }

    sync()
    observeImages()
    editor.on('transaction', sync)
    const observer = typeof MutationObserver === 'undefined'
      ? null
      : new MutationObserver(() => {
          sync()
          observeImages()
        })
    observer?.observe(editorRoot, {
      attributes: true,
      attributeFilter: ['alt', 'data-markdown-target'],
      childList: true,
      subtree: true,
      characterData: true,
    })
    window.addEventListener('resize', sync)

    return () => {
      editor.off('transaction', sync)
      observer?.disconnect()
      resizeObserver?.disconnect()
      window.removeEventListener('resize', sync)
      entriesRef.current = []
    }
  }, [editor, enabled, effectiveReadOnly, hasReplace, isEditableTarget, rootRef])

  if (!editor || effectiveReadOnly || !options || entries.length === 0) return null
  const classNames = options.classNames
  return (
    <div className="pointer-events-none absolute inset-0 z-20" data-markdown-image-menu-layer>
      {entries.map(entry => (
        <div
          key={entry.id}
          data-markdown-image-controls="true"
          data-markdown-image-target={entry.target}
          contentEditable={false}
          className={[DEFAULT_HOST_CLASS_NAME, classNames?.host].filter(Boolean).join(' ')}
          style={{ top: entry.top, left: entry.left, pointerEvents: 'auto' }}
        >
          <ImageMenuActions
            editor={editor}
            image={entry.image}
            alt={entry.alt}
            options={options}
          />
        </div>
      ))}
    </div>
  )
}
