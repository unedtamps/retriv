import {
  GlobalWorkerOptions,
  TextLayer,
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
}

export interface PdfRenderHandle {
  promise: Promise<RenderedPdfPage>
  cancel: () => void
}

export interface PdfTextLayerHandle {
  promise: Promise<void>
  cancel: () => void
}

export async function loadPdf(data: ArrayBuffer): Promise<LoadedPdf> {
  const task = getDocument({
    data,
    // JPEG2000 scans are decoded by OpenJPEG's WASM build. Without this
    // directory PDF.js silently leaves those pages blank.
    wasmUrl: `${import.meta.env.BASE_URL}pdfjs/wasm/`,
  })
  const pdf = await task.promise
  return { pdf, destroy: () => task.destroy() }
}

export function renderPdfPage(
  pdf: PDFDocumentProxy,
  pageNumber: number,
  canvas: HTMLCanvasElement,
  scale: number,
): PdfRenderHandle {
  let cancelled = false
  let task: RenderTask | null = null
  const promise = (async () => {
    const page = await pdf.getPage(pageNumber)
    if (cancelled) throw new Error('Rendering was cancelled.')
    const viewport = page.getViewport({ scale })
    const outputScale = window.devicePixelRatio || 1
    canvas.width = Math.floor(viewport.width * outputScale)
    canvas.height = Math.floor(viewport.height * outputScale)
    const transform =
      outputScale === 1 ? undefined : [outputScale, 0, 0, outputScale, 0, 0]
    task = page.render({
      canvas,
      viewport,
      transform,
    })
    await task.promise
    if (cancelled) throw new Error('Rendering was cancelled.')
    return { width: viewport.width, height: viewport.height }
  })()
  return {
    promise,
    cancel: () => {
      cancelled = true
      try {
        task?.cancel()
      } catch {
        // The task may already have completed.
      }
    },
  }
}

export function renderPdfTextLayer(
  pdf: PDFDocumentProxy,
  pageNumber: number,
  container: HTMLDivElement,
  scale: number,
): PdfTextLayerHandle {
  let cancelled = false
  let layer: TextLayer | null = null
  const promise = (async () => {
    const page = await pdf.getPage(pageNumber)
    if (cancelled) throw new Error('Text layer rendering was cancelled.')
    const viewport = page.getViewport({ scale })
    container.replaceChildren()
    container.style.setProperty('--total-scale-factor', String(scale))
    container.style.setProperty('--scale-round-x', '1px')
    container.style.setProperty('--scale-round-y', '1px')
    layer = new TextLayer({
      textContentSource: page.streamTextContent(),
      container,
      viewport,
    })
    await layer.render()
    if (cancelled) throw new Error('Text layer rendering was cancelled.')
  })()
  return {
    promise,
    cancel: () => {
      cancelled = true
      try {
        layer?.cancel()
      } catch {
        // The layer may already have completed.
      }
    },
  }
}
