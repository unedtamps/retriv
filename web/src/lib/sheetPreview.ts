import type { WorkBook } from 'xlsx'

/**
 * Turning a CSV or a small workbook into markup the viewer can mount.
 *
 * Two formats, one contract: the file's bytes in, an HTML table out. CSV is
 * decoded from text, a workbook from its zip; both then go through the same
 * range clamp and the same sanitising step.
 *
 * The library is imported lazily so its ~300 KB never reaches a user who only
 * opens PDFs: the chunk is fetched the first time a spreadsheet is previewed.
 *
 * A workbook past `PREVIEW_ROW_LIMIT` is refused rather than rendered. Parsing
 * a 200k-row sheet costs ~3 s and ~200 MB of heap and produces tens of
 * megabytes of DOM, which is enough to freeze the tab; the viewer says so and
 * offers the download instead. That limit is a property of the browser, not of
 * the library, which is why it is not raised by configuration.
 */

export const PREVIEW_ROW_LIMIT = 5_000
export const PREVIEW_SHEET_LIMIT = 12

const MARKUP_CACHE_ENTRIES = 3
const MARKUP_CACHE_CHARS = 24 * 1024 * 1024

const markupCache = new Map<string, SheetPreview>()

const BLOCKED_TAGS = new Set(['SCRIPT', 'IFRAME', 'OBJECT', 'EMBED', 'LINK', 'META', 'BASE', 'FORM'])

export interface SheetPreview {
  markup: string
  sheetNames: string[]
  activeSheet: string
  rowCount: number
  truncated: boolean
}

export class SheetTooLargeError extends Error {
  readonly rowCount: number

  constructor(rowCount: number) {
    super(`This spreadsheet holds ${rowCount.toLocaleString()} rows`)
    this.name = 'SheetTooLargeError'
    this.rowCount = rowCount
  }
}

export function cachedSheetMarkup(key: string): SheetPreview | null {
  return markupCache.get(key) ?? null
}

export function clearSheetMarkupCache(): void {
  markupCache.clear()
}

function remember(key: string, preview: SheetPreview): void {
  if (preview.markup.length > MARKUP_CACHE_CHARS) return
  markupCache.set(key, preview)
  while (markupCache.size > MARKUP_CACHE_ENTRIES) {
    const oldest = markupCache.keys().next()
    if (oldest.done === true) return
    markupCache.delete(oldest.value)
  }
}

function rowCountOf(workbook: WorkBook, sheetName: string): number {
  const sheet = workbook.Sheets[sheetName]
  const range = sheet['!ref']
  if (range === undefined) return 0
  const parts = range.split(':')
  const last = parts[1] ?? parts[0]
  const digits = last.replace(/^[A-Z]+/, '')
  const parsed = Number.parseInt(digits, 10)
  return Number.isNaN(parsed) ? 0 : parsed
}

/**
 * The markup for one sheet, clamped to the preview limit.
 *
 * The clamp rewrites the sheet's own `!ref` before rendering rather than
 * truncating the finished HTML: the library then emits only the rows asked for,
 * which is the difference between building 200k DOM rows and discarding them
 * and never building them at all.
 */
function renderSheet(
  XLSX: typeof import('xlsx'),
  workbook: WorkBook,
  sheetName: string,
): SheetPreview {
  const sheetNames = workbook.SheetNames.slice(0, PREVIEW_SHEET_LIMIT)
  const sheet = workbook.Sheets[sheetName]
  const originalRef = sheet['!ref']
  const total = rowCountOf(workbook, sheetName)
  let truncated = false
  if (originalRef !== undefined) {
    const [start] = originalRef.split(':')
    const lastColumn = (originalRef.split(':')[1] ?? originalRef).replace(/\d+$/, '')
    const end = Math.min(total, PREVIEW_ROW_LIMIT)
    sheet['!ref'] = `${start}:${lastColumn}${end}`
    truncated = total > PREVIEW_ROW_LIMIT
  }
  try {
    const markup = XLSX.utils.sheet_to_html(sheet, { editable: false })
    return {
      markup,
      sheetNames,
      activeSheet: sheetName,
      rowCount: total,
      truncated,
    }
  } finally {
    if (originalRef !== undefined) sheet['!ref'] = originalRef
  }
}

async function loadXlsx(): Promise<typeof import('xlsx')> {
  return import('xlsx')
}

/** The preview of a spreadsheet file, or a refusal when it is too large. */
export async function renderSheetPreview(
  key: string,
  blob: Blob,
  format: 'csv' | 'xlsx',
  sheetName?: string,
): Promise<SheetPreview> {
  const XLSX = await loadXlsx()
  const data = format === 'csv' ? await blob.text() : new Uint8Array(await blob.arrayBuffer())
  const workbook =
    format === 'csv'
      ? XLSX.read(data, { type: 'string', raw: false })
      : XLSX.read(data, { type: 'array', cellDates: true })

  if (workbook.SheetNames.length === 0) {
    throw new Error('The file holds no readable sheet')
  }
  const target =
    sheetName !== undefined && workbook.SheetNames.includes(sheetName)
      ? sheetName
      : workbook.SheetNames[0]
  const cached = markupCache.get(cacheKey(key, target))
  if (cached !== undefined) return cached

  const total = rowCountOf(workbook, target)
  if (total > PREVIEW_ROW_LIMIT) throw new SheetTooLargeError(total)

  const preview = renderSheet(XLSX, workbook, target)
  remember(cacheKey(key, target), preview)
  return preview
}

/** The sheet names of a workbook, without rendering any of them. */
export async function listSheets(blob: Blob, format: 'csv' | 'xlsx'): Promise<string[]> {
  const XLSX = await loadXlsx()
  const data = format === 'csv' ? await blob.text() : new Uint8Array(await blob.arrayBuffer())
  const workbook =
    format === 'csv'
      ? XLSX.read(data, { type: 'string' })
      : XLSX.read(data, { type: 'array', bookSheets: true })
  return workbook.SheetNames.slice(0, PREVIEW_SHEET_LIMIT)
}

function cacheKey(key: string, sheetName: string | undefined): string {
  return `${key}:${sheetName ?? ''}`
}

/**
 * The rendered markup with anything executable removed.
 *
 * The library escapes cell text, so a `<script>` in a cell arrives as text
 * rather than as a tag. This pass is the second line: it drops elements that a
 * future library version, an inline style, or a document-embedded hyperlink
 * could reintroduce, and it runs before the markup is ever assigned to the
 * viewer's DOM.
 */
export function sanitizeSheetMarkup(markup: string): string {
  const template = document.createElement('template')
  template.innerHTML = markup
  for (const element of template.content.querySelectorAll('*')) {
    if (BLOCKED_TAGS.has(element.tagName)) {
      element.remove()
      continue
    }
    for (const attribute of element.getAttributeNames()) {
      const name = attribute.toLowerCase()
      if (name.startsWith('on') || name === 'srcdoc') element.removeAttribute(attribute)
      if ((name === 'href' || name === 'src') && /^javascript:/i.test(element.getAttribute(attribute) ?? '')) {
        element.removeAttribute(attribute)
      }
    }
  }
  return template.innerHTML
}
