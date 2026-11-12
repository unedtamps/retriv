import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type RefObject,
} from 'react'
import {
  ChevronLeft,
  ChevronRight,
  LoaderCircle,
  ZoomIn,
  ZoomOut,
} from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import { loadDocument } from '../lib/documentCache'
import {
  loadPdf,
  renderPdfPage,
  renderPdfTextLayer,
  type LoadedPdf,
} from '../lib/pdf'
import type { DocumentUpload, PageTextLine, Workspace } from '../types'

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
  /** Provenance regions to draw over the pages. */
  highlights?: PdfHighlightRegion[]
}

interface PageSize {
  width: number
  height: number
}

const DEFAULT_ZOOM = 1.25
const MIN_ZOOM = 0.5
const MAX_ZOOM = 3
const ZOOM_STEP = 0.25
const FALLBACK_PAGE_SIZE: PageSize = { width: 612, height: 792 }
const PAGE_RENDER_MARGIN = '600px 0px'

function PdfPageView({
  pdf,
  pageNumber,
  scale,
  defaultSize,
  highlights,
  ocrLines,
  scrollRoot,
}: {
  pdf: LoadedPdf['pdf']
  pageNumber: number
  scale: number
  defaultSize: PageSize
  highlights: PdfHighlightRegion[]
  ocrLines: PageTextLine[]
  scrollRoot: RefObject<HTMLDivElement | null>
}) {
  const [visible, setVisible] = useState(
    () => typeof IntersectionObserver === 'undefined',
  )
  const [size, setSize] = useState<PageSize | null>(null)
  const [error, setError] = useState<string | null>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const textLayerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const node = wrapperRef.current
    if (node === null || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.target === node) setVisible(entry.isIntersecting)
        }
      },
      { root: scrollRoot.current, rootMargin: PAGE_RENDER_MARGIN },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [scrollRoot])

  useEffect(() => {
    if (!visible || canvasRef.current === null) return
    const canvas = canvasRef.current
    const textContainer = textLayerRef.current
    let cancelled = false
    let cancel: (() => void) | null = null
    let cancelText: (() => void) | null = null
    renderPdfPage(pdf, pageNumber, canvas, scale)
      .then((rendered) => {
        cancel = rendered.cancel
        if (cancelled) {
          rendered.cancel()
          return
        }
        setSize({ width: rendered.width, height: rendered.height })
        setError(null)
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(getErrorMessage(caught))
      })
    if (textContainer !== null) {
      renderPdfTextLayer(pdf, pageNumber, textContainer, scale)
        .then((rendered) => {
          cancelText = rendered.cancel
          if (cancelled) rendered.cancel()
        })
        .catch(() => {
          // The canvas remains readable when a malformed text layer fails.
        })
    }
    return () => {
      cancelled = true
      if (cancel !== null) cancel()
      if (cancelText !== null) cancelText()
    }
  }, [visible, pdf, pageNumber, scale])

  const pageWidth = (size?.width ?? defaultSize.width * scale)
  const pageHeight = (size?.height ?? defaultSize.height * scale)
  const pageHighlights = highlights.filter(
    (region) => region.page === pageNumber,
  )

  return (
    <div
      ref={wrapperRef}
      className="pdf-page-shell"
      data-page={pageNumber}
      aria-label={`Page ${pageNumber}`}
      style={{
        width: `${pageWidth}px`,
        aspectRatio: `${pageWidth} / ${pageHeight}`,
      }}
    >
      {visible ? (
        <canvas ref={canvasRef} className="pdf-canvas" data-testid="pdf-canvas" />
      ) : null}
      {visible ? (
        <div
          ref={textLayerRef}
          className="pdf-text-layer"
          data-testid="pdf-text-layer"
        />
      ) : null}
      {visible && ocrLines.length > 0 ? (
        <div className="pdf-ocr-layer" data-testid="pdf-ocr-layer">
          {ocrLines.map((line, index) => {
            const [x0, y0, x1, y1] = line.bbox
            const lineHeight = (y1 - y0) * pageHeight
            return (
              <span
                key={index}
                style={{
                  left: `${x0 * 100}%`,
                  top: `${y0 * 100}%`,
                  width: `${(x1 - x0) * 100}%`,
                  height: `${(y1 - y0) * 100}%`,
                  fontSize: `${Math.max(6, lineHeight * 0.8)}px`,
                  lineHeight: 1,
                }}
              >
                {line.text}
              </span>
            )
          })}
        </div>
      ) : null}
      {visible
        ? pageHighlights.map((region, index) => (
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
          ))
        : null}
      {visible && size === null && error === null ? (
        <div className="pdf-loading" role="status">
          <LoaderCircle aria-hidden="true" className="animate-spin" size={17} />
          Rendering page {pageNumber}…
        </div>
      ) : null}
      {error !== null ? (
        <p className="error-note pdf-page-error" role="alert">
          Page {pageNumber}: {error}
        </p>
      ) : null}
    </div>
  )
}

/**
 * A continuous PDF rendered by PDF.js.
 *
 * Pages are placeholders until they approach the viewport, so a long filing
 * does not allocate a canvas per page. The scroll container is the source of
 * truth for the current page; the toolbar only moves that scroll position.
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
  const [defaultSize, setDefaultSize] = useState<PageSize>(FALLBACK_PAGE_SIZE)
  const [zoom, setZoom] = useState(DEFAULT_ZOOM)
  const [error, setError] = useState<string | null>(null)
  const [ocrPages, setOcrPages] = useState<Record<number, PageTextLine[]>>({})
  const [lastPageProp, setLastPageProp] = useState(page)
  const scrollRef = useRef<HTMLDivElement>(null)
  const scrollFrame = useRef<number | null>(null)

  // A second citation can change the target page while the same document is
  // open. Adjust during render instead of reloading the whole PDF.
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
      .then(async (next) => {
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
        try {
          const first = await next.pdf.getPage(1)
          const viewport = first.getViewport({ scale: 1 })
          if (mounted) {
            setDefaultSize({ width: viewport.width, height: viewport.height })
          }
        } catch {
          // The fallback page box is enough until the first page renders.
        }
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
    let mounted = true
    api
      .getDocumentTextLayer(workspace.id, document.id)
      .then(({ pages }) => {
        if (!mounted) return
        setOcrPages(
          Object.fromEntries(
            pages.map((pageLayer) => [pageLayer.page_no, pageLayer.lines]),
          ),
        )
      })
      .catch((caught: unknown) => {
        if (mounted && isUnauthorized(caught)) onUnauthorized()
        // A document without OCR lowers to the ordinary canvas/text layer.
      })
    return () => {
      mounted = false
    }
  }, [document, workspace.id, onUnauthorized])

  const scrollToPage = useCallback((target: number) => {
    const container = scrollRef.current
    if (container === null) return
    const node = container.querySelector<HTMLElement>(`[data-page="${target}"]`)
    node?.scrollIntoView?.({ block: 'start', behavior: 'auto' })
  }, [])

  useEffect(() => {
    if (loadedPdf === null || pageCount === 0) return
    const frame = window.requestAnimationFrame(() => scrollToPage(currentPage))
    return () => window.cancelAnimationFrame(frame)
    // currentPage is intentionally read, not a dependency: user page actions
    // already scroll themselves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedPdf, pageCount, page, scrollToPage])

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => scrollToPage(currentPage))
    return () => window.cancelAnimationFrame(frame)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zoom])

  useEffect(() => {
    return () => {
      if (scrollFrame.current !== null) {
        window.cancelAnimationFrame(scrollFrame.current)
      }
    }
  }, [])

  const handleScroll = useCallback(() => {
    if (scrollFrame.current !== null) return
    scrollFrame.current = window.requestAnimationFrame(() => {
      scrollFrame.current = null
      const container = scrollRef.current
      if (container === null) return
      const top = container.getBoundingClientRect().top
      let next = 1
      for (const element of container.querySelectorAll<HTMLElement>('[data-page]')) {
        const pageTop = element.getBoundingClientRect().top - top
        if (pageTop <= container.clientHeight * 0.35) {
          next = Number(element.dataset.page ?? next)
        } else {
          break
        }
      }
      setCurrentPage(next)
    })
  }, [])

  const commitPage = (input: HTMLInputElement) => {
    const parsed = Number.parseInt(input.value, 10)
    if (!Number.isFinite(parsed) || pageCount === 0) {
      input.value = String(currentPage)
      return
    }
    const next = Math.min(Math.max(1, parsed), pageCount)
    input.value = String(next)
    setCurrentPage(next)
    scrollToPage(next)
  }

  const goTo = (target: number) => {
    const next = Math.min(Math.max(1, target), pageCount)
    setCurrentPage(next)
    scrollToPage(next)
  }

  const changeZoom = (next: number) => {
    setZoom(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, next)))
  }

  return (
    <div className="pdf-viewer" data-testid="pdf-viewer">
      <div className="pdf-toolbar">
        <button
          type="button"
          className="btn-icon"
          aria-label="Previous page"
          disabled={loadedPdf === null || currentPage <= 1}
          onClick={() => goTo(currentPage - 1)}
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
          onClick={() => goTo(currentPage + 1)}
        >
          <ChevronRight aria-hidden="true" size={16} />
        </button>

        <span className="pdf-toolbar-spacer" />

        <button
          type="button"
          className="btn-icon"
          aria-label="Zoom out"
          disabled={loadedPdf === null || zoom <= MIN_ZOOM}
          onClick={() => changeZoom(zoom - ZOOM_STEP)}
        >
          <ZoomOut aria-hidden="true" size={16} />
        </button>
        <span className="pdf-zoom">{Math.round(zoom * 100)}%</span>
        <button
          type="button"
          className="btn-icon"
          aria-label="Zoom in"
          disabled={loadedPdf === null || zoom >= MAX_ZOOM}
          onClick={() => changeZoom(zoom + ZOOM_STEP)}
        >
          <ZoomIn aria-hidden="true" size={16} />
        </button>
      </div>

      {error !== null ? (
        <p className="error-note pdf-error" role="alert">
          {error}
        </p>
      ) : loadedPdf === null ? (
        <div className="pdf-loading pdf-loading-full" role="status">
          <LoaderCircle aria-hidden="true" className="animate-spin" size={17} />
          Loading the PDF…
        </div>
      ) : (
        <div
          ref={scrollRef}
          className="pdf-scroll"
          onScroll={handleScroll}
          data-testid="pdf-scroll"
        >
          {Array.from({ length: pageCount }, (_, index) => {
            const pageNumber = index + 1
            return (
              <PdfPageView
                key={pageNumber}
                pdf={loadedPdf.pdf}
                pageNumber={pageNumber}
                scale={zoom}
                defaultSize={defaultSize}
                highlights={highlights}
                ocrLines={ocrPages[pageNumber] ?? []}
                scrollRoot={scrollRef}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}
