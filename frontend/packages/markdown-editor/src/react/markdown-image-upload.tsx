import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type {
  ChangeEvent,
  ClipboardEvent,
  DragEvent,
  ReactNode,
} from 'react'
import type { Editor } from '@tiptap/core'
import { NodeSelection, TextSelection } from '@tiptap/pm/state'
import { ImagePlus, Loader2, RotateCcw, X } from 'lucide-react'

import { uploadMarkdownBatch } from '../adapters.js'
import { markdownCommands } from '../core.js'
import type {
  MarkdownAsset,
  MarkdownUploadAdapter,
  MarkdownUploadContext,
  MarkdownUploadItem,
} from '../types.js'

export interface MarkdownImageUploadLabels {
  group: string
  insert: string
  uploading: string
  checking: (name: string) => string
  cancel: string
  failed: string
  queued: string
  retry: string
  upload: string
  chooseAnother: string
  dismiss: string
  successful: string
  cancelled: (count: number) => string
  remaining: (count: number) => string
  previousBatchFinished: string
}

export interface MarkdownImageUploadClassNames {
  status?: string
  message?: string
  actions?: string
  action?: string
  input?: string
}

export interface MarkdownImageUploadOptions {
  adapter: MarkdownUploadAdapter
  context?: Omit<MarkdownUploadContext, 'signal'>
  accept?: string
  labels?: Partial<MarkdownImageUploadLabels>
  classNames?: MarkdownImageUploadClassNames
  onUploadingChange?: (uploading: boolean) => void
  onAssetUploaded?: (asset: MarkdownAsset, file: Blob) => void
  onAssetReplaced?: (previousTarget: string, asset: MarkdownAsset, file: Blob) => void
}

export interface MarkdownImageUploadFailure {
  kind: 'error' | 'queued'
  message: string
  retryFiles: readonly File[]
  queuedFiles: readonly File[]
}

export interface MarkdownImageUploadState {
  uploading: boolean
  currentFileName: string
  completed: number
  total: number
  failure: MarkdownImageUploadFailure | null
}

interface UploadAnchor {
  kind: 'insert' | 'replace'
  bookmark: ReturnType<Editor['state']['selection']['getBookmark']>
  previousTarget?: string
}

interface InternalFailure extends MarkdownImageUploadFailure {
  anchor: UploadAnchor
}

interface ActiveUpload {
  controller: AbortController
  anchor: UploadAnchor
}

export interface MarkdownImageUploadController {
  state: MarkdownImageUploadState
  labels: MarkdownImageUploadLabels
  setInputElement: (element: HTMLInputElement | null) => void
  openPicker: () => void
  beginReplacement: (position: number) => void
  selectFiles: (files: readonly File[]) => void
  cancel: () => void
  retry: () => void
  uploadQueued: () => void
  chooseAnother: () => void
  dismiss: () => void
  handleDragOver: (event: DragEvent<HTMLDivElement>) => void
  handleDrop: (event: DragEvent<HTMLDivElement>) => void
  handlePaste: (event: ClipboardEvent<HTMLDivElement>) => void
}

export const DEFAULT_MARKDOWN_IMAGE_UPLOAD_LABELS: MarkdownImageUploadLabels = {
  group: 'Attachments',
  insert: 'Insert image',
  uploading: 'Uploading image',
  checking: name => `Checking ${name}`,
  cancel: 'Cancel upload',
  failed: 'Image upload failed',
  queued: 'Images waiting to upload',
  retry: 'Retry',
  upload: 'Upload',
  chooseAnother: 'Choose another',
  dismiss: 'Dismiss',
  successful: 'Successful images remain in the draft.',
  cancelled: count => `${count} image${count === 1 ? '' : 's'} cancelled.`,
  remaining: count => `${count} image${count === 1 ? '' : 's'} remain in this batch.`,
  previousBatchFinished: 'The previous image batch finished. Upload the next batch when ready.',
}

const DEFAULT_STATUS_CLASS_NAME =
  'flex flex-wrap items-center justify-between gap-3 border-x border-b border-border bg-surface-2 px-4 py-3 text-sm text-foreground'
const DEFAULT_ACTION_CLASS_NAME =
  'inline-flex min-h-9 items-center justify-center gap-1.5 rounded-[var(--radius-sm)] border border-border bg-surface px-3 text-sm font-medium text-foreground transition-token hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-50'

const UploadContext = createContext<MarkdownImageUploadController | null>(null)

function filesFromTransfer(transfer: DataTransfer | null | undefined): File[] {
  return Array.from(transfer?.files ?? []).filter(file => file.type.startsWith('image/'))
}

function isStandaloneImageClipboard(transfer: DataTransfer | null | undefined): boolean {
  const files = filesFromTransfer(transfer)
  if (!files.length) return false

  const html = transfer?.getData('text/html') ?? ''
  if (!html.trim()) {
    const plain = (transfer?.getData('text/plain') ?? '').trim()
    if (!plain) return true
    const imageNames = new Set(files.map(file => file.name))
    const lines = plain
      .split(/\r?\n/)
      .map(line => line.trim())
      .filter(Boolean)
    return lines.length > 0 && lines.every(line => imageNames.has(line))
  }

  if (!/<img\b/i.test(html) || typeof document === 'undefined') return false
  const template = document.createElement('template')
  template.innerHTML = html
  for (const node of template.content.querySelectorAll('img, meta, link, style')) node.remove()
  return !(template.content.textContent ?? '').trim()
}

function fileName(file: Blob): string {
  return file instanceof File && file.name ? file.name : 'image'
}

function anchorAtSelection(editor: Editor): UploadAnchor {
  return {
    kind: 'insert',
    bookmark: editor.state.selection.getBookmark(),
  }
}

function anchorAtDrop(editor: Editor, event: DragEvent<HTMLDivElement>): UploadAnchor {
  const position = editor.view.posAtCoords({ left: event.clientX, top: event.clientY })?.pos
  if (position === undefined) return anchorAtSelection(editor)
  try {
    return {
      kind: 'insert',
      bookmark: TextSelection.create(editor.state.doc, position).getBookmark(),
    }
  } catch {
    return anchorAtSelection(editor)
  }
}

function errorForPlacement(message: string) {
  return {
    code: 'invalid' as const,
    message,
    retryable: false,
  }
}

export function useMarkdownImageUpload(
  editor: Editor | null,
  options: MarkdownImageUploadOptions | undefined,
  readOnly = false,
): MarkdownImageUploadController | null {
  const inputRef = useRef<HTMLInputElement>(null)
  const optionsRef = useRef(options)
  const readOnlyRef = useRef(readOnly)
  const mountedRef = useRef(true)
  const anchorsRef = useRef(new Set<UploadAnchor>())
  const pendingAnchorRef = useRef<UploadAnchor | null>(null)
  const activeRef = useRef<ActiveUpload | null>(null)
  const queuedRef = useRef<Array<{ files: File[]; anchor: UploadAnchor }>>([])
  const failureRef = useRef<InternalFailure | null>(null)
  const [state, setState] = useState<MarkdownImageUploadState>({
    uploading: false,
    currentFileName: '',
    completed: 0,
    total: 0,
    failure: null,
  })

  useEffect(() => {
    optionsRef.current = options
    readOnlyRef.current = readOnly
  }, [options, readOnly])

  useEffect(() => {
    mountedRef.current = true
    const anchors = anchorsRef.current
    return () => {
      mountedRef.current = false
      activeRef.current?.controller.abort()
      activeRef.current = null
      pendingAnchorRef.current = null
      queuedRef.current = []
      anchors.clear()
    }
  }, [editor])

  useEffect(() => {
    if (!editor) return
    const anchors = anchorsRef.current

    const mapAnchors = ({ transaction }: { transaction: { docChanged: boolean; mapping: Parameters<UploadAnchor['bookmark']['map']>[0] } }) => {
      if (!transaction.docChanged) return
      for (const anchor of anchors) {
        anchor.bookmark = anchor.bookmark.map(transaction.mapping)
      }
    }
    editor.on('transaction', mapAnchors)
    return () => {
      editor.off('transaction', mapAnchors)
    }
  }, [editor])

  const enabled = Boolean(editor && options?.adapter && !readOnly)
  const labels = useMemo(
    () => ({ ...DEFAULT_MARKDOWN_IMAGE_UPLOAD_LABELS, ...options?.labels }),
    [options?.labels],
  )

  const registerAnchor = useCallback((anchor: UploadAnchor): UploadAnchor => {
    anchorsRef.current.add(anchor)
    return anchor
  }, [])

  const makeInsertAnchor = useCallback((): UploadAnchor | null => {
    if (!editor || editor.isDestroyed) return null
    return registerAnchor(anchorAtSelection(editor))
  }, [editor, registerAnchor])

  const makeReplacementAnchor = useCallback((position: number): UploadAnchor | null => {
    if (!editor || editor.isDestroyed || !Number.isInteger(position)) return null
    const node = editor.state.doc.nodeAt(position)
    if (!node || node.type.name !== 'image') return null
    try {
      return registerAnchor({
        kind: 'replace',
        bookmark: NodeSelection.create(editor.state.doc, position).getBookmark(),
        previousTarget: typeof node.attrs.target === 'string' ? node.attrs.target : undefined,
      })
    } catch {
      return null
    }
  }, [editor, registerAnchor])

  const setStateIfMounted = useCallback((next: (current: MarkdownImageUploadState) => MarkdownImageUploadState) => {
    if (mountedRef.current) setState(next)
  }, [])

  const placeAsset = useCallback((asset: MarkdownAsset, file: Blob, anchor: UploadAnchor) => {
    const currentOptions = optionsRef.current
    currentOptions?.onAssetUploaded?.(asset, file)

    if (!editor || editor.isDestroyed || !editor.isEditable || !mountedRef.current) return null
    const target = typeof asset.target === 'string' ? asset.target.trim() : ''
    if (!target) {
      return errorForPlacement('The image upload returned an invalid asset target.')
    }

    const alt = asset.alt || (file instanceof File ? file.name.replace(/\.[^.]+$/, '') : '') || 'Image'
    const commands = markdownCommands(editor)
    let selection
    try {
      selection = anchor.bookmark.resolve(editor.state.doc)
    } catch {
      return errorForPlacement('The original image position is no longer available.')
    }

    if (anchor.kind === 'replace') {
      if (!(selection instanceof NodeSelection) || selection.node.type.name !== 'image') {
        return errorForPlacement('The original image position is no longer available.')
      }
      const position = selection.from
      const previousTarget = anchor.previousTarget
      if (!commands.replaceImageAt(position, target, alt, asset.title)) {
        return errorForPlacement('The original image position is no longer available.')
      }
      if (previousTarget && previousTarget !== target) {
        currentOptions?.onAssetReplaced?.(previousTarget, asset, file)
      }
      anchor.kind = 'insert'
      anchor.previousTarget = undefined
      try {
        const nextPosition = Math.min(position + 1, editor.state.doc.content.size)
        editor.commands.setTextSelection({ from: nextPosition, to: nextPosition })
      } catch {
        // The next item can still resolve the mapped bookmark if a text
        // selection cannot be placed immediately after an atomic node.
      }
      anchor.bookmark = editor.state.selection.getBookmark()
      return null
    }

    try {
      editor.view.dispatch(editor.state.tr.setSelection(selection))
    } catch {
      return errorForPlacement('The image insertion position is no longer available.')
    }
    if (!commands.insertImage(target, alt, asset.title)) {
      return errorForPlacement('The image insertion position is no longer available.')
    }
    anchor.bookmark = editor.state.selection.getBookmark()
    return null
  }, [editor])

  const startBatch = useCallback((files: readonly File[], anchor: UploadAnchor) => {
    const currentOptions = optionsRef.current
    if (!enabled || !editor || !currentOptions?.adapter || !files.length || activeRef.current) return

    const controller = new AbortController()
    activeRef.current = { controller, anchor }
    failureRef.current = null
    setStateIfMounted(() => ({
      uploading: true,
      currentFileName: '',
      completed: 0,
      total: files.length,
      failure: null,
    }))
    currentOptions.onUploadingChange?.(true)

    void (async () => {
      try {
        const result = await uploadMarkdownBatch(
          currentOptions.adapter,
          files,
          {
            ...currentOptions.context,
            target: anchor.kind === 'replace' ? anchor.previousTarget : undefined,
            signal: controller.signal,
          },
          {
            onFileStart: (file, index, total) => {
              setStateIfMounted(current => ({
                ...current,
                currentFileName: fileName(file),
                completed: index,
                total,
              }))
            },
            onFileSettled: (_item, index, total) => {
              setStateIfMounted(current => ({ ...current, completed: index + 1, total }))
            },
          },
        )

        const placementFailures: Array<{ file: File; error: ReturnType<typeof errorForPlacement> }> = []
        for (const item of result.items) {
          if (item.status !== 'success') continue
          const error = placeAsset(item.asset, item.file, anchor)
          if (error) placementFailures.push({ file: item.file as File, error })
        }

        const retryFiles = result.items
          .filter((item): item is Extract<MarkdownUploadItem, { status: 'failed' | 'cancelled' }> =>
            item.status !== 'success' && item.error.retryable,
          )
          .map(item => item.file as File)
        const placementRetryFiles = placementFailures
          .filter(failure => failure.error.retryable)
          .map(failure => failure.file)
        const queued = queuedRef.current.splice(0)
        const queuedFiles = queued.flatMap(batch => batch.files)
        const hasErrors = result.failed > 0 || result.cancelled > 0 || placementFailures.length > 0
        const batchLabels = {
          ...DEFAULT_MARKDOWN_IMAGE_UPLOAD_LABELS,
          ...currentOptions.labels,
        }
        const messages = [
          ...result.items
            .filter(item => item.status !== 'success')
            .map(item => item.error.message),
          ...placementFailures.map(failure => failure.error.message),
          result.cancelled ? batchLabels.cancelled(result.cancelled) : '',
          (result.succeeded || placementFailures.length) && (hasErrors || queuedFiles.length)
            ? batchLabels.successful
            : '',
          queuedFiles.length ? batchLabels.previousBatchFinished : '',
        ].filter(Boolean)

        if (messages.length || queuedFiles.length) {
          const currentFailure: InternalFailure = {
            kind: hasErrors ? 'error' : 'queued',
            message: messages.join(' '),
            retryFiles: [...retryFiles, ...placementRetryFiles],
            queuedFiles,
            anchor,
          }
          failureRef.current = currentFailure
          setStateIfMounted(current => ({
            ...current,
            uploading: false,
            currentFileName: '',
            failure: currentFailure,
          }))
        } else {
          anchorsRef.current.delete(anchor)
          setStateIfMounted(current => ({
            ...current,
            uploading: false,
            currentFileName: '',
            failure: null,
          }))
        }
      } finally {
        activeRef.current = null
        if (mountedRef.current) currentOptions.onUploadingChange?.(false)
      }
    })()
  }, [editor, enabled, placeAsset, setStateIfMounted])

  const selectFilesAtAnchor = useCallback((files: readonly File[], suppliedAnchor?: UploadAnchor) => {
    if (!enabled || !files.length) return
    const anchor = suppliedAnchor
      ? registerAnchor(suppliedAnchor)
      : pendingAnchorRef.current ?? makeInsertAnchor()
    pendingAnchorRef.current = null
    if (!anchor) return
    const selectedFiles = anchor.kind === 'replace' ? files.slice(0, 1) : files
    if (!selectedFiles.length) return
    if (activeRef.current) {
      queuedRef.current.push({ files: [...selectedFiles], anchor })
      return
    }
    startBatch(selectedFiles, anchor)
  }, [enabled, makeInsertAnchor, registerAnchor, startBatch])

  const selectFiles = useCallback((files: readonly File[]) => {
    selectFilesAtAnchor(files)
  }, [selectFilesAtAnchor])

  const openPicker = useCallback(() => {
    if (!enabled || activeRef.current) return
    pendingAnchorRef.current = makeInsertAnchor()
    if (inputRef.current) inputRef.current.multiple = true
    inputRef.current?.click()
  }, [enabled, makeInsertAnchor])

  const beginReplacement = useCallback((position: number) => {
    if (!enabled || activeRef.current) return
    const anchor = makeReplacementAnchor(position)
    if (!anchor) return
    pendingAnchorRef.current = anchor
    if (inputRef.current) inputRef.current.multiple = false
    inputRef.current?.click()
  }, [enabled, makeReplacementAnchor])

  const cancel = useCallback(() => {
    activeRef.current?.controller.abort()
  }, [])

  const retry = useCallback(() => {
    const failure = failureRef.current
    if (!failure || !failure.retryFiles.length || activeRef.current) return
    if (failure.queuedFiles.length) {
      queuedRef.current.unshift({ files: [...failure.queuedFiles], anchor: failure.anchor })
    }
    failureRef.current = null
    setStateIfMounted(current => ({ ...current, failure: null }))
    startBatch(failure.retryFiles, failure.anchor)
  }, [setStateIfMounted, startBatch])

  const uploadQueued = useCallback(() => {
    const failure = failureRef.current
    if (!failure || !failure.queuedFiles.length || activeRef.current) return
    if (failure.retryFiles.length) {
      queuedRef.current.unshift({ files: [...failure.retryFiles], anchor: failure.anchor })
    }
    failureRef.current = null
    setStateIfMounted(current => ({ ...current, failure: null }))
    startBatch(failure.queuedFiles, failure.anchor)
  }, [setStateIfMounted, startBatch])

  const chooseAnother = useCallback(() => {
    const failure = failureRef.current
    if (!failure || activeRef.current) return
    if (failure.queuedFiles.length) {
      queuedRef.current.unshift({ files: [...failure.queuedFiles], anchor: failure.anchor })
    }
    pendingAnchorRef.current = failure.anchor
    failureRef.current = null
    setStateIfMounted(current => ({ ...current, failure: null }))
    inputRef.current?.click()
  }, [setStateIfMounted])

  const dismiss = useCallback(() => {
    const failure = failureRef.current
    if (failure) anchorsRef.current.delete(failure.anchor)
    failureRef.current = null
    pendingAnchorRef.current = null
    queuedRef.current = []
    setStateIfMounted(current => ({ ...current, failure: null }))
  }, [setStateIfMounted])

  const handleDragOver = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!enabled || readOnlyRef.current) return
    const hasImage = filesFromTransfer(event.dataTransfer).length > 0 ||
      Array.from(event.dataTransfer.items ?? []).some(item => item.kind === 'file' && item.type.startsWith('image/'))
    if (!hasImage) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
  }, [enabled])

  const handleDrop = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!enabled || readOnlyRef.current) return
    const files = filesFromTransfer(event.dataTransfer)
    if (!files.length) return
    event.preventDefault()
    event.stopPropagation()
    selectFilesAtAnchor(files, anchorAtDrop(editor!, event))
  }, [editor, enabled, selectFilesAtAnchor])

  const handlePaste = useCallback((event: ClipboardEvent<HTMLDivElement>) => {
    if (!enabled || readOnlyRef.current || !isStandaloneImageClipboard(event.clipboardData)) return
    const files = filesFromTransfer(event.clipboardData)
    if (!files.length) return
    event.preventDefault()
    event.stopPropagation()
    selectFiles(files)
  }, [enabled, selectFiles])

  const controller = useMemo<MarkdownImageUploadController>(() => ({
    state,
    labels,
    setInputElement: element => {
      inputRef.current = element
    },
    openPicker,
    beginReplacement,
    selectFiles,
    cancel,
    retry,
    uploadQueued,
    chooseAnother,
    dismiss,
    handleDragOver,
    handleDrop,
    handlePaste,
  }), [
    beginReplacement,
    cancel,
    chooseAnother,
    dismiss,
    handleDragOver,
    handleDrop,
    handlePaste,
    openPicker,
    retry,
    selectFiles,
    state,
    uploadQueued,
    labels,
  ])

  return enabled ? controller : null
}

export function MarkdownImageUploadProvider({
  controller,
  children,
}: {
  controller: MarkdownImageUploadController
  children: ReactNode
}) {
  return <UploadContext.Provider value={controller}>{children}</UploadContext.Provider>
}

export function useMarkdownImageUploadContext(): MarkdownImageUploadController | null {
  return useContext(UploadContext)
}

export function MarkdownImageUploadInput({
  accept,
  className,
}: {
  accept?: string
  className?: string
}) {
  const controller = useMarkdownImageUploadContext()
  if (!controller) return null
  return (
    <input
      ref={element => controller.setInputElement(element)}
      type="file"
      accept={accept ?? 'image/*'}
      className={className ?? 'sr-only'}
      tabIndex={-1}
      aria-hidden="true"
      onChange={(event: ChangeEvent<HTMLInputElement>) => {
        const files = Array.from(event.currentTarget.files ?? [])
        event.currentTarget.value = ''
        controller.selectFiles(files)
      }}
    />
  )
}

export function MarkdownImageUploadStatus({
  options,
}: {
  options?: MarkdownImageUploadOptions
}) {
  const controller = useMarkdownImageUploadContext()
  if (!controller) return null
  const labels = { ...DEFAULT_MARKDOWN_IMAGE_UPLOAD_LABELS, ...options?.labels }
  const classNames = options?.classNames
  const current = controller.state
  const failure = current.failure
  const actionClassName = [DEFAULT_ACTION_CLASS_NAME, classNames?.action].filter(Boolean).join(' ')

  if (!current.uploading && !failure) return null
  if (current.uploading) {
    return (
      <div className={[DEFAULT_STATUS_CLASS_NAME, classNames?.status].filter(Boolean).join(' ')} data-markdown-image-upload-status>
        <div className="min-w-0 flex-1" role="status" aria-live="polite">
          <div className="font-medium">{labels.uploading}</div>
          <div className={classNames?.message ?? 'truncate text-foreground-muted'}>
            {current.currentFileName ? labels.checking(current.currentFileName) : labels.uploading}
            {current.total > 1 ? ` (${current.completed}/${current.total})` : ''}
          </div>
        </div>
        <button type="button" className={actionClassName} onClick={controller.cancel}>
          <X className="h-3.5 w-3.5" aria-hidden />
          {labels.cancel}
        </button>
      </div>
    )
  }

  if (!failure) return null
  return (
    <div
      className={[DEFAULT_STATUS_CLASS_NAME, classNames?.status].filter(Boolean).join(' ')}
      role="alert"
      data-markdown-image-upload-status
    >
      <div className="min-w-0 flex-1">
        <div className="font-medium">{failure.kind === 'queued' ? labels.queued : labels.failed}</div>
        <div className={classNames?.message ?? 'text-foreground-muted'}>
          {failure.message || labels.previousBatchFinished}
          {failure.retryFiles.length + failure.queuedFiles.length > 0
            ? ` ${labels.remaining(failure.retryFiles.length + failure.queuedFiles.length)}`
            : ''}
        </div>
      </div>
      <div className={["flex flex-wrap items-center gap-1.5", classNames?.actions].filter(Boolean).join(' ')}>
        {failure.retryFiles.length > 0 && (
          <button type="button" className={actionClassName} onClick={controller.retry}>
            <RotateCcw className="h-3.5 w-3.5" aria-hidden />
            {labels.retry}
          </button>
        )}
        {failure.queuedFiles.length > 0 && (
          <button type="button" className={actionClassName} onClick={controller.uploadQueued}>
            <Loader2 className="hidden h-3.5 w-3.5" aria-hidden />
            {labels.upload}
          </button>
        )}
        <button type="button" className={actionClassName} onClick={controller.chooseAnother}>
          <ImagePlus className="h-3.5 w-3.5" aria-hidden />
          {labels.chooseAnother}
        </button>
        <button type="button" className="inline-flex min-h-9 items-center justify-center rounded-[var(--radius-sm)] px-3 text-sm font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface" onClick={controller.dismiss}>
          {labels.dismiss}
        </button>
      </div>
    </div>
  )
}
