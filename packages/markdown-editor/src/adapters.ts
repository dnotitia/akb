import type {
  MarkdownAdapterError,
  MarkdownAsset,
  MarkdownTarget,
  MarkdownTargetResolution,
  MarkdownTargetResolver,
  MarkdownTargetResolverContext,
  MarkdownUnavailableReason,
  MarkdownUploadAdapter,
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
): Promise<MarkdownUploadBatchResult> {
  const items: MarkdownUploadItem[] = []

  for (const file of files) {
    if (context.signal?.aborted) {
      items.push(cancelledItem(file))
      continue
    }

    try {
      const asset = await adapter.upload(file, context)
      // An adapter may finish a remote upload after the caller aborts its
      // signal. The returned asset is still real and must remain visible to
      // the caller so it can be claimed or explicitly discarded. The signal
      // gates the next file; it does not erase a completed result.
      items.push({ status: 'success', file, asset })
    } catch (error) {
      const normalized = adapterError(error)
      if (normalized.code === 'cancelled' || context.signal?.aborted) {
        items.push({ status: 'cancelled', file, error: normalized })
        continue
      }
      items.push({ status: 'failed', file, error: normalized })
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

export function targetResolution(
  target: MarkdownTarget,
  runtimeUrl: string,
  label?: string,
): MarkdownTargetResolution {
  return {
    target: target.target,
    kind: target.kind,
    status: 'available',
    runtimeUrl,
    label,
  }
}

export function unavailableTargetResolution(
  target: MarkdownTarget,
  reason: MarkdownUnavailableReason = 'unknown',
  label?: string,
): MarkdownTargetResolution {
  return {
    target: target.target,
    kind: target.kind,
    status: 'unavailable',
    reason,
    label,
  }
}

export function isMarkdownAsset(value: unknown): value is MarkdownAsset {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<MarkdownAsset>
  return (
    (candidate.kind === 'file' || candidate.kind === 'attachment') &&
    typeof candidate.target === 'string' &&
    candidate.target.trim().length > 0
  )
}
