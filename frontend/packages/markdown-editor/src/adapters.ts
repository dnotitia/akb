import type {
  MarkdownAdapterError,
  MarkdownTarget,
  MarkdownTargetResolution,
  MarkdownTargetResolver,
  MarkdownTargetResolverContext,
  MarkdownUploadAdapter,
  MarkdownUploadBatchOptions,
  MarkdownUploadBatchResult,
  MarkdownUploadContext,
  MarkdownUploadItem,
} from './types.js'

function adapterError(error: unknown): MarkdownAdapterError {
  if (error instanceof DOMException && error.name === 'AbortError') {
    return {
      code: 'cancelled',
      message: 'Upload cancelled',
      retryable: true,
    }
  }

  if (error && typeof error === 'object') {
    const candidate = error as {
      code?: unknown
      message?: unknown
      retryable?: unknown
    }
    const code = candidate.code
    if (
      code === 'unavailable' ||
      code === 'invalid' ||
      code === 'conflict' ||
      code === 'cancelled'
    ) {
      return {
        code,
        message:
          typeof candidate.message === 'string' && candidate.message
            ? candidate.message
            : 'The upload could not be completed.',
        retryable: candidate.retryable === true,
      }
    }
    return {
      code: 'unknown',
      message:
        typeof candidate.message === 'string' && candidate.message
          ? candidate.message
          : 'The upload could not be completed.',
      retryable: candidate.retryable === true,
    }
  }

  return {
    code: 'unknown',
    message: error instanceof Error ? error.message : 'The upload could not be completed.',
    retryable: false,
  }
}

function cancelledItem(file: Blob): MarkdownUploadItem {
  return {
    status: 'cancelled',
    file,
    error: adapterError(new DOMException('Upload cancelled', 'AbortError')),
  }
}

/**
 * Run independent file uploads through the one-file adapter contract while
 * retaining every outcome. A failed item never erases a successful item, and
 * cancellation marks the current/remaining files explicitly.
 */
export async function uploadMarkdownBatch(
  adapter: MarkdownUploadAdapter,
  files: readonly Blob[],
  context: MarkdownUploadContext = {},
  options: MarkdownUploadBatchOptions = {},
): Promise<MarkdownUploadBatchResult> {
  const items: MarkdownUploadItem[] = []

  for (const [index, file] of files.entries()) {
    options.onFileStart?.(file, index, files.length)
    if (context.signal?.aborted) {
      const item = cancelledItem(file)
      items.push(item)
      options.onFileSettled?.(item, index, files.length)
      continue
    }

    try {
      const asset = await adapter.upload(file, context)
      // An adapter may finish a remote upload after the caller aborts its
      // signal. The returned asset is still real and must remain visible to
      // the caller so it can be claimed or explicitly discarded. The signal
      // gates the next file; it does not erase a completed result.
      const item = { status: 'success', file, asset } satisfies MarkdownUploadItem
      items.push(item)
      options.onFileSettled?.(item, index, files.length)
    } catch (error) {
      const normalized = adapterError(error)
      if (normalized.code === 'cancelled' || context.signal?.aborted) {
        const item = { status: 'cancelled', file, error: normalized } satisfies MarkdownUploadItem
        items.push(item)
        options.onFileSettled?.(item, index, files.length)
        continue
      }
      const item = { status: 'failed', file, error: normalized } satisfies MarkdownUploadItem
      items.push(item)
      options.onFileSettled?.(item, index, files.length)
    }
  }

  const succeeded = items.filter(item => item.status === 'success').length
  const failed = items.filter(item => item.status === 'failed').length
  const cancelled = items.filter(item => item.status === 'cancelled').length

  return {
    items,
    succeeded,
    failed,
    cancelled,
    partial: items.length > 0 && succeeded > 0 && (failed > 0 || cancelled > 0),
  }
}

/**
 * Resolve each distinct canonical target once. Resolver failures become a
 * coarse unavailable result so one broken resource cannot blank a document.
 */
export async function resolveMarkdownTargets(
  resolver: MarkdownTargetResolver,
  targets: readonly MarkdownTarget[] | readonly string[],
  context: MarkdownTargetResolverContext = {},
): Promise<ReadonlyMap<string, MarkdownTargetResolution>> {
  const unique = new Map<string, MarkdownTarget>()
  for (const value of targets) {
    const target = typeof value === 'string' ? value : value.target
    if (!unique.has(target)) {
      unique.set(target, typeof value === 'string' ? { kind: 'document', target } : value)
    }
  }

  const entries = await Promise.all(
    [...unique.values()].map(async target => {
      try {
        return [
          target.target,
          await resolver.resolve(target.target, {
            vault: context.vault,
            document: context.document,
            commit: context.commit,
            signal: context.signal,
          }),
        ] as const
      } catch {
        return [
          target.target,
          {
            target: target.target,
            kind: target.kind,
            status: 'unavailable',
            reason: 'unknown',
          } satisfies MarkdownTargetResolution,
        ] as const
      }
    }),
  )

  return new Map(entries)
}
