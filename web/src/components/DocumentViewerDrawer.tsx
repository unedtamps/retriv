import { useEffect, useState } from 'react'
import { Download, ExternalLink, LoaderCircle, Maximize2, Minimize2 } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import { cachedDocument, loadDocument, type DocumentFile } from '../lib/documentCache'
import type { DocumentUpload, Workspace } from '../types'
import { Dialog } from './Dialog'
import { DocumentDocxPreview } from './DocumentDocxPreview'
import { DocumentJsonPreview } from './DocumentJsonPreview'
import { DocumentSheetPreview } from './DocumentSheetPreview'

interface DocumentViewerDrawerProps {
  document: DocumentUpload
  workspace: Workspace
  onUnauthorized: () => void
  onClose: () => void
}

const DOCX_MEDIA_TYPE =
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
const XLSX_MEDIA_TYPE =
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

/**
 * The original document, rendered in place.
 *
 * The bytes are fetched once per document and kept, so closing and re-opening
 * the same file is instant and costs no request. A second open reads the
 * cached entry synchronously and never shows a spinner.
 */
export function DocumentViewerDrawer({
  document,
  workspace,
  onUnauthorized,
  onClose,
}: DocumentViewerDrawerProps) {
  const [loaded, setLoaded] = useState<DocumentFile | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  const cached = cachedDocument(document)
  const file = loaded ?? cached
  const isPdf = document.detected_media_type === 'application/pdf'
  const isDocx = document.detected_media_type === DOCX_MEDIA_TYPE
  const isJson = document.detected_media_type === 'application/json'
  const isSheet =
    document.detected_media_type === 'text/csv' ||
    document.detected_media_type === XLSX_MEDIA_TYPE

  useEffect(() => {
    if (cachedDocument(document) !== null) return
    // Deliberately not abortable. The fetch is owned by the cache and shared
    // between callers, so aborting it on effect cleanup (which StrictMode runs
    // immediately after mount) would leave a dead promise in the in-flight map
    // and the viewer stuck on "Loading" forever. A mounted flag is enough to
    // ignore a result that arrives after the drawer closed.
    let mounted = true
    loadDocument(document, () => api.fetchDocumentBlob(workspace.id, document.id))
      .then((next) => {
        if (mounted) setLoaded(next)
      })
      .catch((caught: unknown) => {
        if (!mounted) return
        if (isUnauthorized(caught)) onUnauthorized()
        else setError(getErrorMessage(caught))
      })
    return () => {
      mounted = false
    }
  }, [document, workspace.id, onUnauthorized])

  const ready = file !== null

  return (
    <Dialog
      open
      variant="wide"
      fullScreen={expanded}
      eyebrow={workspace.name}
      title={document.original_filename}
      onClose={onClose}
      headerActions={
        <>
          <button
            type="button"
            className="btn-icon"
            aria-label={
              expanded
                ? `Restore ${document.original_filename} to a window`
                : `Expand ${document.original_filename} to full screen`
            }
            aria-pressed={expanded}
            onClick={() => setExpanded((current) => !current)}
          >
            {expanded ? (
              <Minimize2 aria-hidden="true" size={17} />
            ) : (
              <Maximize2 aria-hidden="true" size={17} />
            )}
          </button>
          <a
            className="btn-icon"
            href={file?.url ?? '#'}
            download={document.original_filename}
            aria-label={`Download ${document.original_filename}`}
            aria-disabled={!ready}
            onClick={(event) => {
              if (!ready) event.preventDefault()
            }}
          >
            <Download aria-hidden="true" size={17} />
          </a>
          <a
            className="btn-icon"
            href={file?.url ?? '#'}
            target="_blank"
            rel="noreferrer"
            aria-label={`Open ${document.original_filename} in a new tab`}
            aria-disabled={!ready}
            onClick={(event) => {
              if (!ready) event.preventDefault()
            }}
          >
            <ExternalLink aria-hidden="true" size={17} />
          </a>
        </>
      }
    >
      <div className="min-h-0 flex-1 overflow-hidden bg-paper">
        {error !== null ? (
          <p className="error-note" role="alert">
            {error}
          </p>
        ) : isDocx ? (
          <DocumentDocxPreview
            document={document}
            workspace={workspace}
            onUnauthorized={onUnauthorized}
          />
        ) : isJson ? (
          <DocumentJsonPreview
            document={document}
            workspace={workspace}
            onUnauthorized={onUnauthorized}
          />
        ) : isSheet ? (
          <DocumentSheetPreview
            document={document}
            workspace={workspace}
            onUnauthorized={onUnauthorized}
          />
        ) : file === null ? (
          <div
            className="flex h-full items-center justify-center gap-2 text-sm text-ink-muted"
            role="status"
          >
            <LoaderCircle aria-hidden="true" className="animate-spin" size={17} />
            Loading preview…
          </div>
        ) : isPdf ? (
          <iframe
            src={file.url}
            title={document.original_filename}
            className="h-full w-full border-0 bg-surface"
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
            <p className="text-sm font-medium text-ink">No inline preview for this file type</p>
            <p className="max-w-sm text-sm leading-6 text-ink-muted">
              {document.detected_media_type} cannot be rendered in the browser. Download it to open
              it locally.
            </p>
          </div>
        )}
      </div>
    </Dialog>
  )
}
