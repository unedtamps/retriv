import { describe, expect, it } from 'vitest'
import {
  PREVIEW_ROW_LIMIT,
  SheetTooLargeError,
  clearSheetMarkupCache,
  renderSheetPreview,
  sanitizeSheetMarkup,
} from './sheetPreview'

function csvBlob(text: string): Blob {
  return new Blob([text], { type: 'text/csv' })
}

function csvWithRows(count: number): string {
  const lines = ['id,name,amount']
  for (let index = 0; index < count; index += 1) {
    lines.push(`${index},item ${index},${index * 10}`)
  }
  return lines.join('\n')
}

describe('sheet preview', () => {
  it('renders a CSV as a table with its header row', async () => {
    clearSheetMarkupCache()
    const preview = await renderSheetPreview(
      'csv-1',
      csvBlob('id,status,amount\n1,OPEN,1500\n2,CLOSED,250'),
      'csv',
    )
    expect(preview.markup).toContain('<table')
    expect(preview.markup).toContain('status')
    expect(preview.markup).toContain('CLOSED')
    expect(preview.rowCount).toBe(3)
    expect(preview.truncated).toBe(false)
  })

  it('keeps a quoted comma and an embedded newline inside one cell', async () => {
    clearSheetMarkupCache()
    const preview = await renderSheetPreview(
      'csv-2',
      csvBlob('name,note\n"Smith, John",plain\n"two\nlines",other'),
      'csv',
    )
    expect(preview.markup).toContain('Smith, John')
    // A newline inside a cell is emitted as the markup that draws one, so the
    // two halves stay in the same cell rather than becoming separate rows.
    expect(preview.markup).toContain('two<br/>lines')
  })

  it('refuses a sheet past the row limit instead of rendering it', async () => {
    clearSheetMarkupCache()
    await expect(
      renderSheetPreview('csv-big', csvBlob(csvWithRows(PREVIEW_ROW_LIMIT + 10)), 'csv'),
    ).rejects.toBeInstanceOf(SheetTooLargeError)
  })

  it('reports the real row count in the refusal so the UI can say it', async () => {
    clearSheetMarkupCache()
    const oversized = PREVIEW_ROW_LIMIT + 25
    const failure = await renderSheetPreview(
      'csv-big-2',
      csvBlob(csvWithRows(oversized)),
      'csv',
    ).catch((caught: unknown) => caught)
    expect(failure).toBeInstanceOf(SheetTooLargeError)
    expect((failure as SheetTooLargeError).rowCount).toBe(oversized + 1)
  })

  it('renders a sheet at exactly the limit', async () => {
    clearSheetMarkupCache()
    const preview = await renderSheetPreview(
      'csv-edge',
      csvBlob(csvWithRows(PREVIEW_ROW_LIMIT - 1)),
      'csv',
    )
    expect(preview.rowCount).toBe(PREVIEW_ROW_LIMIT)
    expect(preview.truncated).toBe(false)
  })

  it('serves a repeat request from the cache without reparsing', async () => {
    clearSheetMarkupCache()
    const blob = csvBlob('a,b\n1,2')
    const first = await renderSheetPreview('csv-cached', blob, 'csv')
    const second = await renderSheetPreview('csv-cached', blob, 'csv')
    expect(second).toBe(first)
  })
})

describe('sheet markup sanitising', () => {
  it('drops a script element a cell tried to introduce', () => {
    const dirty = '<table><tr><td>ok</td></tr></table><script>alert(1)</script>'
    expect(sanitizeSheetMarkup(dirty)).not.toContain('<script')
  })

  it('strips event handler attributes', () => {
    const dirty = '<table><tr><td onclick="alert(1)">cell</td></tr></table>'
    const clean = sanitizeSheetMarkup(dirty)
    expect(clean).not.toContain('onclick')
    expect(clean).toContain('cell')
  })

  it('strips a javascript: link but keeps the text', () => {
    const dirty = '<table><tr><td><a href="javascript:alert(1)">label</a></td></tr></table>'
    const clean = sanitizeSheetMarkup(dirty)
    expect(clean).not.toContain('javascript:')
    expect(clean).toContain('label')
  })

  it('leaves ordinary table markup alone', () => {
    const clean = '<table><tr><td data-t="s">12</td></tr></table>'
    expect(sanitizeSheetMarkup(clean)).toContain('data-t="s"')
  })
})

function workbookBlob(rows: number, sheets = ['Data']): Promise<Blob> {
  return import('xlsx').then((XLSX) => {
    const workbook = XLSX.utils.book_new()
    for (const name of sheets) {
      const data: (string | number)[][] = [['id', 'name', 'amount']]
      for (let index = 0; index < rows; index += 1) {
        data.push([index, `item ${index}`, index * 10])
      }
      XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet(data), name)
    }
    const buffer = XLSX.write(workbook, { bookType: 'xlsx', type: 'array' }) as ArrayBuffer
    return new Blob([buffer], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
  })
}

describe('workbook preview', () => {
  it('renders a small workbook', async () => {
    clearSheetMarkupCache()
    const preview = await renderSheetPreview('wb-1', await workbookBlob(3), 'xlsx')
    expect(preview.markup).toContain('item 0')
    expect(preview.rowCount).toBe(4)
  })

  it('lists every sheet so the viewer can offer tabs', async () => {
    clearSheetMarkupCache()
    const preview = await renderSheetPreview(
      'wb-2',
      await workbookBlob(2, ['Sales', 'Meta']),
      'xlsx',
    )
    expect(preview.sheetNames).toEqual(['Sales', 'Meta'])
    expect(preview.activeSheet).toBe('Sales')
  })

  it('renders a named sheet when asked for one', async () => {
    clearSheetMarkupCache()
    const blob = await workbookBlob(2, ['Sales', 'Meta'])
    const preview = await renderSheetPreview('wb-3', blob, 'xlsx', 'Meta')
    expect(preview.activeSheet).toBe('Meta')
  })

  it('clamps a sheet that sits just under the limit without marking it truncated', async () => {
    clearSheetMarkupCache()
    const preview = await renderSheetPreview('wb-4', await workbookBlob(PREVIEW_ROW_LIMIT - 1), 'xlsx')
    expect(preview.truncated).toBe(false)
    expect(preview.rowCount).toBe(PREVIEW_ROW_LIMIT)
  })

  it('refuses a workbook past the limit before building any DOM', async () => {
    clearSheetMarkupCache()
    await expect(
      renderSheetPreview('wb-big', await workbookBlob(PREVIEW_ROW_LIMIT + 1), 'xlsx'),
    ).rejects.toBeInstanceOf(SheetTooLargeError)
  })
})
