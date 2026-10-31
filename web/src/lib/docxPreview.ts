import type { Options } from 'docx-preview'

/**
 * Rendering a `.docx` into markup the viewer can mount.
 *
 * The library is imported lazily so its ~100 KB never reaches a user who only
 * opens PDFs: the chunk is fetched the first time a Word file is previewed.
 *
 * The rendered markup is kept per file hash, because parsing a Word package is
 * the expensive part (unzip, XML parse, style resolution, layout) and the
 * result for a given set of bytes never changes. Re-opening a document that was
 * already previewed in this tab costs nothing but a string assignment.
 */
const RENDER_OPTIONS: Partial<Options> = {
  className: 'retriv-docx',
  inWrapper: true,
  breakPages: true,
  ignoreLastRenderedPageBreak: true,
  renderHeaders: true,
  renderFooters: true,
  renderFootnotes: true,
  renderEndnotes: true,
  // Comments and tracked changes are editing affordances, not document content.
  renderComments: false,
  renderChanges: false,
  // An altChunk is an embedded HTML part that the library mounts as an
  // iframe srcdoc. That is a document-originated page inside our origin, and
  // nothing a retrieval product needs to show.
  renderAltChunks: false,
  // Inline the images and embedded fonts as data URLs rather than object
  // URLs. Object URLs created during a render are never handed back to us, so
  // they would leak for the lifetime of the tab; a data URL dies with the
  // markup.
  useBase64URL: true,
  experimental: false,
  debug: false,
  ignoreWidth: false,
  ignoreHeight: false,
  ignoreFonts: false,
}

const MARKUP_CACHE_ENTRIES = 3
const MARKUP_CACHE_CHARS = 24 * 1024 * 1024

const markupCache = new Map<string, string>()

const BLOCKED_TAGS = new Set(['SCRIPT', 'IFRAME', 'OBJECT', 'EMBED', 'LINK', 'META', 'BASE', 'FORM'])

/** The rendered representation of a document, ready to mount. */
export async function renderDocxMarkup(key: string, blob: Blob): Promise<string> {
  const cached = cachedDocxMarkup(key)
  if (cached !== null) return cached

  const { renderAsync } = await import('docx-preview')
  const host = document.createElement('div')
  // Attached but out of sight: the renderer measures text to place tab stops,
  // and those measurements are only real for a laid-out subtree.
  host.style.cssText =
    'position:fixed;top:0;left:-10000px;width:640px;visibility:hidden;pointer-events:none'
  host.setAttribute('aria-hidden', 'true')

  const styles = document.createElement('div')
  const body = document.createElement('div')
  host.append(styles, body)
  document.body.appendChild(host)

  let markup: string
  try {
    await renderAsync(blob, body, styles, RENDER_OPTIONS)
    sanitize(styles)
    sanitize(body)
    markup = `${styles.innerHTML}${body.innerHTML}`
  } catch {
    throw new Error('This Word document could not be rendered. Download it to open it locally.')
  } finally {
    host.remove()
  }

  if (markup.trim() === '') {
    throw new Error('This Word document has no content to render.')
  }

  storeMarkup(key, markup)
  return markup
}

/** A document's markup if it has already been rendered in this tab. */
export function cachedDocxMarkup(key: string): string | null {
  const markup = markupCache.get(key)
  if (markup === undefined) return null
  // Touch: re-insert so the most recently used entry is last in insertion order.
  markupCache.delete(key)
  markupCache.set(key, markup)
  return markup
}

/** Forget every rendered document. Called on sign-out. */
export function clearDocxMarkupCache(): void {
  markupCache.clear()
}

function storeMarkup(key: string, markup: string): void {
  markupCache.set(key, markup)
  while (
    markupCache.size > MARKUP_CACHE_ENTRIES ||
    totalMarkupChars() > MARKUP_CACHE_CHARS
  ) {
    const oldest = markupCache.keys().next()
    if (oldest.done === true) break
    markupCache.delete(oldest.value)
  }
}

function totalMarkupChars(): number {
  let total = 0
  for (const markup of markupCache.values()) total += markup.length
  return total
}

/**
 * Strip everything from the rendered tree that a hostile file could use.
 *
 * The document is HTML by the time we see it, and no attribute of it was
 * authored by us, so the rules are deny-by-default: no scripting tags, no
 * event handlers, no embedded documents, and only URLs we can explain.
 */
function sanitize(container: HTMLElement): void {
  for (const element of [...container.querySelectorAll('*')]) {
    if (BLOCKED_TAGS.has(element.tagName)) {
      element.remove()
      continue
    }
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase()
      if (name.startsWith('on') || name === 'srcdoc' || name === 'formaction') {
        element.removeAttribute(attribute.name)
        continue
      }
      if (name === 'href' || name === 'src' || name === 'xlink:href') {
        const isImageSource = name === 'src' || name === 'xlink:href'
        if (!isAllowedUrl(attribute.value, isImageSource)) {
          element.removeAttribute(attribute.name)
        }
      }
    }
    if (element.tagName === 'STYLE') {
      element.textContent = stripRemoteImports(element.textContent ?? '')
    }
  }
}

function isAllowedUrl(value: string, allowImageData: boolean): boolean {
  const url = value.trim().toLowerCase()
  if (url === '' || url.startsWith('#')) return true
  if (url.startsWith('http://') || url.startsWith('https://') || url.startsWith('mailto:')) {
    return true
  }
  return allowImageData && url.startsWith('data:image/')
}

function stripRemoteImports(cssText: string): string {
  return cssText.replace(/@import[^;]*;/gi, '')
}
