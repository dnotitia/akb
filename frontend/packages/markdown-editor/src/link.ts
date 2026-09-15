import type { MarkdownLinkUrlNormalizer } from './types.js'

const BARE_DOMAIN = /^[\w.-]+\.[a-z]{2,}(?:[/:?#]|$)/i
const SAFE_SCHEME = /^(?:https?|mailto|tel|akb):/i

/**
 * Normalize the product-neutral set of link destinations accepted by the
 * popup. Products may provide a stricter or canonicalizing policy to the
 * popup; this default still rejects active-content and protocol-relative URLs.
 */
export const normalizeMarkdownLinkUrl: MarkdownLinkUrlNormalizer = raw => {
  const trimmed = raw.trim()
  if (!trimmed || trimmed.startsWith('//')) return null

  const withScheme = BARE_DOMAIN.test(trimmed) ? `https://${trimmed}` : trimmed
  if (withScheme.startsWith('/') || withScheme.startsWith('#') || withScheme.startsWith('?')) {
    return withScheme
  }
  return SAFE_SCHEME.test(withScheme) ? withScheme : null
}
