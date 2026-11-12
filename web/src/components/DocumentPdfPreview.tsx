import { useEffect, useRef, useState } from 'react'
import {
  ChevronLeft,
  ChevronRight,
  LoaderCircle,
  ZoomIn,
  ZoomOut,
} from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import { loadDocument } from '../lib/documentCache'
import { loadPdf, renderPdfPage, type LoadedPdf } from '../lib/pdf'
import type { DocumentUpload, Workspace } from '../types'

export interface PdfHighlightRegion {
  /** 1-based page number. */
  page: number
  /** Normalized 0..1 coordinates relative to the page box. */
  x: number
  y: number
  width: number
  height: number
}

interface DocumentPdfPreviewProps {
  document: DocumentUpload
  workspace: Workspace
  onUnauthorized: () => void
  /** Page to open first, used by a citation. */
  page?: number
  /** Provenance regions to draw over the current page. */
  highlights?: PdfHighlightRegion[]
}

const DEFAULT_ZOOM = 1.25
const MIN_ZOOM = 0.5
const MAX_ZOOM = 3
const ZOOM_STEP = 0.25

/**
 * A PDF rendered by PDF.js instead of the browser's built-in frame.
 *
 * The native viewer can jump to a page, but it cannot draw application-owned
 * highlights. PDF.js gives us the page canvas, so provenance rectangles can be
 * added without searching the PDF's text layer -- OCR and VLM text need not
 * exist in the file for the region overlay to be correct.
 */
export function DocumentPdfPreview({
  document,
  workspace,
  onUnauthorized,
  page,
  highlights = [],
}: DocumentPdfPreviewProps) {
  const [loadedPdf, setLoadedPdf] = useState<LoadedPdf | null>(null)
  const [pageCount, setPageCount] = useState(0)
  const [currentPage, setCurrentPage] = useState(() =>
    page === undefined ? 1 : Math.max(1, page),
  )
  const [zoom, setZoom] = useState(DEFAULT_ZOOM)
  const [pageSize, setPageSize] = useState<{ width: number; height: number } | null>(
    null,
  )
  const [error, setError] = useState<string | null>(null)
  const [lastPageProp, setLastPageProp] = useState(page)
  const canvasRef = useRef<HTMLCanvasElement>(null)

  // Adjusting state during render is the documented React escape hatch for a
  // prop that arrives from outside (a second citation click on the same PDF).
  if (page !== lastPageProp) {
    setLastPageProp(page)
    setCurrentPage(page === undefined ? 1 : Math.max(1, page))
  }

  useEffect(() => {
    let mounted = true
    let loaded: LoadedPdf | null = null

    loadDocument(document, () =>
      api.fetchDocumentBlob(workspace.id, document.id),
    )
      .then(({ blob }) => blob.arrayBuffer())
      .then((bytes) => loadPdf(bytes))
      .then((next) => {
        loaded = next
        if (!mounted) {
          void next.destroy()
          return
        }
        setLoadedPdf(next)
        setPageCount(next.pdf.numPages)
        setCurrentPage((value) =>
          Math.min(Math.max(1, value), next.pdf.numPages),
        )
      })
      .catch((caught: unknown) => {
        if (!mounted) return
        if (isUnauthorized(caught)) onUnauthorized()
        else setError(getErrorMessage(caught))
      })

    return () => {
      mounted = false
      if (loaded !== null) void loaded.destroy()
    }
  }, [document, workspace.id, onUnauthorized])

  useEffect(() => {
    if (loadedPdf === null) return
    const canvas = canvasRef.current
    if (canvas === null) return
    let cancelled = false
    let cancel: (() => void) | null = null
    renderPdfPage(loadedPdf.pdf, currentPage, canvas, zoom)
      .then((rendered) => {
        cancel = rendered.cancel
        if (cancelled) {
          rendered.cancel()
          return
        }
        setPageSize({ width: rendered.width, height: rendered.height })
        setError(null)
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        setError(getErrorMessage(caught))
      })
    return () => {
      cancelled = true
      if (cancel !== null) cancel()
    }
  }, [loadedPdf, currentPage, zoom])

  const pageHighlights = highlights.filter(
    (region) => region.page === currentPage,
  )

  const commitPage = (input: HTMLInputElement) => {
    const parsed = Number.parseInt(input.value, 10)
    if (!Number.isFinite(parsed) || pageCount === 0) {
      input.value = String(currentPage)
      return
    }
    const next = Math.min(Math.max(1, parsed), pageCount)
    input.value = String(next)
    setCurrentPage(next)
  }

  return (
    <div className="pdf-viewer" data-testid="pdf-viewer">
      <div className="pdf-toolbar">
        <button
          type="button"
          className="btn-icon"
          aria-label="Previous page"
          disabled={loadedPdf === null || currentPage <= 1}
          onClick={() => setCurrentPage((value) => Math.max(1, value - 1))}
        >
          <ChevronLeft aria-hidden="true" size={16} />
        </button>
        <span className="pdf-page-count">
          <input
            key={currentPage}
            className="pdf-page-input"
            type="number"
            min={1}
            max={Math.max(1, pageCount)}
            defaultValue={currentPage}
            disabled={pageCount === 0}
            aria-label="Page number"
            onBlur={(event) => commitPage(event.currentTarget)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return
              event.preventDefault()
              commitPage(event.currentTarget)
              event.currentTarget.blur()
            }}
          />
          <span aria-live="polite">
            {pageCount === 0 ? 'of …' : `of ${pageCount}`}
          </span>
        </span>
        <button
          type="button"
          className="btn-icon"
          aria-label="Next page"
          disabled={loadedPdf === null || currentPage >= pageCount}
          onClick={() =>
            setCurrentPage((value) => Math.min(pageCount, value + 1))
          }
        >
          <ChevronRight aria-hidden="true" size={16} />
        </button>

        <span className="pdf-toolbar-spacer" />

        <button
          type="button"
          className="btn-icon"
          aria-label="Zoom out"
          disabled={loadedPdf === null || zoom <= MIN_ZOOM}
          onClick={() => setZoom((value) => Math.max(MIN_ZOOM, value - ZOOM_STEP))}
        >
          <ZoomOut aria-hidden="true" size={16} />
        </button>
        <span className="pdf-zoom">{Math.round(zoom * 100)}%</span>
        <button
          type="button"
          className="btn-icon"
          aria-label="Zoom in"
          disabled={loadedPdf === null || zoom >= MAX_ZOOM}
          onClick={() => setZoom((value) => Math.min(MAX_ZOOM, value + ZOOM_STEP))}
        >
          <ZoomIn aria-hidden="true" size={16} />
        </button>
      </div>

      {error !== null ? (
        <p className="error-note pdf-error" role="alert">
          {error}
        </p>
      ) : (
        <div className="pdf-scroll">
          <div
            className="pdf-page-shell"
            style={pageSize === null ? undefined : { width: `${pageSize.width}px` }}
          >
            <canvas ref={canvasRef} className="pdf-canvas" data-testid="pdf-canvas" />
            {pageHighlights.map((region, index) => (
              <span
                className="pdf-highlight"
                key={index}
                style={{
                  left: `${region.x * 100}%`,
                  top: `${region.y * 100}%`,
                  width: `${region.width * 100}%`,
                  height: `${region.height * 100}%`,
                }}
              />
            ))}
            {loadedPdf === null || pageSize === null ? (
              <div className="pdf-loading" role="status">
                <LoaderCircle aria-hidden="true" className="animate-spin" size={17} />
                Rendering the page…
              </div>
            ) : null}
          </div>
        </div>
      )}
    </div>
  )
}
