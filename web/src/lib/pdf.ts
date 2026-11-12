import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentProxy,
  type RenderTask,
} from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

GlobalWorkerOptions.workerSrc = workerUrl

export interface LoadedPdf {
  pdf: PDFDocumentProxy
  destroy: () => Promise<void>
}

export interface RenderedPdfPage {
  width: number
  height: number
  cancel: () => void
}

/**
 * Load a PDF from the bytes the document cache already fetched.
 *
 * The worker is a Vite asset rather than a CDN URL: the dashboard has to keep
 * working on a private deployment with no outbound access. The loading task
 * owns ``destroy``; the document proxy does not.
 */
export async function loadPdf(data: ArrayBuffer): Promise<LoadedPdf> {
  const task = getDocument({ data })
  const pdf = await task.promise
  return { pdf, destroy: () => task.destroy() }
}

/**
 * Paint one page onto a canvas at the requested scale.
 *
 * The pixel ratio is applied to the backing canvas, not to the CSS box, so a
 * page stays sharp on a retina display while its layout remains in CSS pixels.
 */
export async function renderPdfPage(
  pdf: PDFDocumentProxy,
  pageNumber: number,
  canvas: HTMLCanvasElement,
  scale: number,
): Promise<RenderedPdfPage> {
  const page = await pdf.getPage(pageNumber)
  const viewport = page.getViewport({ scale })
  const outputScale = window.devicePixelRatio || 1
  canvas.width = Math.floor(viewport.width * outputScale)
  canvas.height = Math.floor(viewport.height * outputScale)
  const transform =
    outputScale === 1 ? undefined : [outputScale, 0, 0, outputScale, 0, 0]
  const task: RenderTask = page.render({
    canvas,
    viewport,
    transform,
  })
  await task.promise
  return {
    width: viewport.width,
    height: viewport.height,
    cancel: () => task.cancel(),
  }
}
