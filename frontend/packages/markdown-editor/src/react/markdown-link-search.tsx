import { useCallback, useId, useLayoutEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

import type {
  MarkdownSearchAdapter,
  MarkdownSearchContext,
  MarkdownSearchResult,
} from '../types.js'

export interface MarkdownLinkSearchLabels {
  inputLabel: string
  inputPlaceholder: string
  searching: string
  empty: string
  error: string
  retry: string
  results: string
  document: string
  file: string
  resource: string
}

interface MarkdownLinkSearchProps {
  adapter: MarkdownSearchAdapter
  context?: Omit<MarkdownSearchContext, 'signal'>
  labels?: Partial<MarkdownLinkSearchLabels>
  className?: string
  inputRef: RefObject<HTMLInputElement | null>
  onSelect: (result: MarkdownSearchResult) => void
}

interface MarkdownSearchState {
  adapter: MarkdownSearchAdapter
  vault?: string
  query: string
  revision: number
  status: 'idle' | 'empty' | 'error' | 'results'
  results: readonly MarkdownSearchResult[]
}

const DEFAULT_LABELS: MarkdownLinkSearchLabels = {
  inputLabel: 'Search resources',
  inputPlaceholder: 'Find a document or file',
  searching: 'Searching…',
  empty: 'No documents or files found.',
  error: 'Search failed. Try again.',
  retry: 'Retry search',
  results: 'Search results',
  document: 'Document',
  file: 'File',
  resource: 'Resource',
}

const inputClass =
  'flex h-10 w-full rounded-[var(--radius-md)] border border-border bg-surface px-3 py-2 text-sm text-foreground placeholder:text-foreground-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface aria-[invalid=true]:border-destructive aria-[invalid=true]:focus-visible:ring-destructive'

const buttonClass =
  'inline-flex h-9 items-center justify-center gap-2 rounded-[var(--radius-md)] border border-border px-4 text-sm font-medium text-foreground transition-token hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-50'

function joinClasses(...classes: Array<string | undefined>): string {
  return classes.filter(Boolean).join(' ')
}

function isAbortError(error: unknown): boolean {
  return Boolean(
    error &&
      typeof error === 'object' &&
      'name' in error &&
      error.name === 'AbortError',
  )
}

export function MarkdownLinkSearch({
  adapter,
  context,
  labels,
  className,
  inputRef,
  onSelect,
}: MarkdownLinkSearchProps) {
  const copy = { ...DEFAULT_LABELS, ...labels }
  const [query, setQuery] = useState('')
  const [searchState, setSearchState] = useState<MarkdownSearchState | null>(null)
  const [activeIndex, setActiveIndex] = useState(-1)
  const [revision, setRevision] = useState(0)
  const controllerRef = useRef<AbortController | null>(null)
  const generationRef = useRef(0)
  const inputId = useId()
  const resultsId = useId()
  const statusId = useId()
  const optionId = (index: number) => `${resultsId}-option-${index}`
  const vault = context?.vault
  const normalizedQuery = query.trim()
  const currentSearchState =
    searchState &&
    searchState.adapter === adapter &&
    searchState.vault === vault &&
    searchState.query === normalizedQuery &&
    searchState.revision === revision
      ? searchState
      : null
  const status = normalizedQuery ? currentSearchState?.status ?? 'loading' : 'idle'
  const results = currentSearchState?.status === 'results' ? currentSearchState.results : []

  const cancel = useCallback(() => {
    generationRef.current += 1
    controllerRef.current?.abort()
    controllerRef.current = null
  }, [])

  const selectResult = (result: MarkdownSearchResult) => {
    cancel()
    setQuery('')
    setActiveIndex(-1)
    onSelect(result)
  }

  useLayoutEffect(() => {
    if (!normalizedQuery) {
      cancel()
      return
    }

    const controller = new AbortController()
    const generation = ++generationRef.current
    controllerRef.current = controller
    const requestRevision = revision

    const isCurrent = () =>
      !controller.signal.aborted && generation === generationRef.current

    void Promise.resolve()
      .then(() => adapter.search(normalizedQuery, { vault, signal: controller.signal }))
      .then((nextResults) => {
        if (!isCurrent()) return
        setActiveIndex(-1)
        setSearchState({
          adapter,
          vault,
          query: normalizedQuery,
          revision: requestRevision,
          status: nextResults.length > 0 ? 'results' : 'empty',
          results: nextResults,
        })
      })
      .catch((error: unknown) => {
        if (!isCurrent()) return
        setSearchState({
          adapter,
          vault,
          query: normalizedQuery,
          revision: requestRevision,
          status: isAbortError(error) ? 'idle' : 'error',
          results: [],
        })
      })

    return () => {
      controller.abort()
      if (controllerRef.current === controller) controllerRef.current = null
      if (generationRef.current === generation) generationRef.current += 1
    }
  }, [adapter, cancel, normalizedQuery, revision, vault])

  return (
    <div className={joinClasses('space-y-2', className)} aria-busy={status === 'loading'}>
      <label htmlFor={inputId} className="text-sm font-medium leading-none">
        {copy.inputLabel}
      </label>
      <input
        ref={inputRef}
        id={inputId}
        role="combobox"
        aria-autocomplete="list"
        aria-haspopup="listbox"
        aria-controls={resultsId}
        aria-expanded={results.length > 0}
        aria-describedby={status === 'loading' || status === 'empty' || status === 'error' ? statusId : undefined}
        aria-activedescendant={activeIndex >= 0 ? optionId(activeIndex) : undefined}
        value={query}
        onChange={(event) => {
          const nextQuery = event.target.value
          cancel()
          setQuery(nextQuery)
          setRevision((value) => value + 1)
          setActiveIndex(-1)
        }}
        onKeyDown={(event) => {
          if (results.length === 0) return
          if (event.key === 'ArrowDown') {
            event.preventDefault()
            setActiveIndex((index) => (index + 1) % results.length)
          } else if (event.key === 'ArrowUp') {
            event.preventDefault()
            setActiveIndex((index) =>
              index < 0 ? results.length - 1 : (index - 1 + results.length) % results.length,
            )
          } else if (event.key === 'Enter' && activeIndex >= 0) {
            event.preventDefault()
            const result = results[activeIndex]
            if (result) selectResult(result)
          }
        }}
        placeholder={copy.inputPlaceholder}
        className={inputClass}
      />
      {status === 'loading' && (
        <p id={statusId} role="status" className="text-xs text-foreground-muted">
          {copy.searching}
        </p>
      )}
      {status === 'empty' && (
        <p id={statusId} role="status" className="text-xs text-foreground-muted">
          {copy.empty}
        </p>
      )}
      {status === 'error' && (
        <div className="flex items-center justify-between gap-2">
          <p id={statusId} role="alert" className="text-xs text-destructive">
            {copy.error}
          </p>
          <button
            type="button"
            className={buttonClass}
            onClick={() => {
              setRevision((value) => value + 1)
              requestAnimationFrame(() => inputRef.current?.focus())
            }}
          >
            {copy.retry}
          </button>
        </div>
      )}
      {results.length > 0 && (
        <div
          id={resultsId}
          role="listbox"
          aria-label={copy.results}
          className="max-h-40 overflow-y-auto rounded-[var(--radius-md)] border border-border"
        >
          {results.map((result, index) => {
            const kindLabel = result.kind === 'document'
              ? copy.document
              : result.kind === 'file'
                ? copy.file
                : copy.resource
            return (
              <button
                id={optionId(index)}
                key={result.id}
                type="button"
                role="option"
                aria-selected={activeIndex === index}
                aria-label={`${result.title} (${result.kind ?? 'resource'})`}
                className={joinClasses(
                  'flex w-full flex-col items-start gap-0.5 border-b border-border px-3 py-2 text-left last:border-b-0 hover:bg-surface-hover focus-visible:bg-surface-hover focus-visible:outline-none',
                  activeIndex === index
                    ? 'bg-surface-selected text-surface-selected-foreground'
                    : undefined,
                )}
                onMouseDown={(event) => event.preventDefault()}
                onMouseMove={() => setActiveIndex(index)}
                onClick={() => selectResult(result)}
              >
                {result.title}
                <span className="text-xs text-foreground-muted">
                  {kindLabel}{result.snippet ? ` · ${result.snippet}` : ''}
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
