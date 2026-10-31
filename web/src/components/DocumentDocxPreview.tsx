import { useEffect, useRef, useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import { loadDocument } from '../lib/documentCache'
import { cachedDocxMarkup, renderDocxMarkup } from '../lib/docxPreview'
import type { DocumentUpload, Workspace } from '../types'

interface DocumentDocxPreviewProps {
  document: DocumentUpload
  workspace: Workspace
  onUnauthorized: () => void
}

/**
 * A Word document, laid out the way Word would lay it out.
 *
 * The rendered pages live inside a shadow root for two reasons: the stylesheet
 * that comes with the file describes generic selectors (`p`, `table`, `span`)
 * and would otherwise repaint the whole application, and the application's own
 * styles would otherwise wreck the page. Neither direction is wanted.
 *
 * Inside the shadow root the wrapper chrome still reads the application's
 * colour tokens -- custom properties inherit across a shadow boundary -- so the
 * surrounding surface follows the theme while the pages themselves stay paper
 * white, which is what a document is supposed to look like in either theme.
 */
export function DocumentDocxPreview({
  document,
  workspace,
  onUnauthorized,
}: DocumentDocxPreviewProps) {
  const [markup, setMarkup] = useState<string | null>(() => cachedDocxMarkup(document.sha256))
  const [error, setError] = useState<string | null>(null)
  const hostRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // Already rendered in this tab: the markup is in hand and the file does
    // not need to be fetched again at all.
    const alreadyRendered = cachedDocxMarkup(document.sha256)
    if (alreadyRendered !== null) return
    // Deliberately not abortable, for the same reason as the PDF path: the
    // fetch belongs to the document cache and is shared, so StrictMode's
    // mount-then-cleanup would cancel a load the reopened drawer still needs.
    let mounted = true
    loadDocument(document, () => api.fetchDocumentBlob(workspace.id, document.id))
      .then(({ blob }) => renderDocxMarkup(document.sha256, blob))
      .then((next) => {
        if (mounted) setMarkup(next)
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

  useEffect(() => {
    const host = hostRef.current
    if (host === null || markup === null) return
    const root = host.shadowRoot ?? host.attachShadow({ mode: 'open' })
    // The chrome goes last so its rules win every specificity tie against the
    // stylesheet that came with the document.
    root.innerHTML = `${markup}${SHELL_STYLES}`
  }, [markup])

  if (error !== null) {
    return (
      <p className="error-note" role="alert">
        {error}
      </p>
    )
  }

  if (markup === null) {
    return (
      <div
        className="flex h-full items-center justify-center gap-2 text-sm text-ink-muted"
        role="status"
      >
        <LoaderCircle aria-hidden="true" className="animate-spin" size={17} />
        Laying out {document.original_filename}…
      </div>
    )
  }

  return <div ref={hostRef} className="h-full w-full" data-testid="docx-preview" />
}

/**
 * Chrome for the rendered pages, inside the shadow root.
 *
 * `.retriv-docx-wrapper` and the page `section` are docx-preview's own class
 * names; the two-class selectors here beat its single-class rules regardless of
 * which stylesheet was inserted first.
 */
const SHELL_STYLES = `
<style>
:host {
  display: block;
  height: 100%;
  overflow: auto;
  overscroll-behavior: contain;
  background: var(--color-paper);
}
.retriv-docx-wrapper {
  background: transparent;
  padding: 20px 12px 32px;
  gap: 20px;
}
@media (min-width: 768px) {
  .retriv-docx-wrapper {
    padding: 28px 24px 40px;
  }
}
.retriv-docx-wrapper > section.retriv-docx {
  background: #ffffff;
  box-shadow: 0 1px 2px rgb(0 0 0 / 0.28), 0 12px 32px rgb(0 0 0 / 0.22);
  border: 1px solid rgb(0 0 0 / 0.08);
  margin-bottom: 0;
}
/*
 * A phone cannot show a fixed A4 page without a horizontal scroll, so below
 * the tablet width the page stops being a page and the content flows to the
 * screen instead. The page width and the one-inch margins are inline styles
 * written by the renderer, which is why this needs !important to win.
 */
@media (max-width: 640px) {
  .retriv-docx-wrapper {
    padding: 10px 8px 20px;
  }
  .retriv-docx-wrapper > section.retriv-docx {
    width: auto !important;
    min-height: 0 !important;
    max-width: 100%;
    padding-left: 14px !important;
    padding-right: 14px !important;
    padding-top: 18px !important;
    padding-bottom: 18px !important;
  }
}
.retriv-docx-wrapper > section.retriv-docx a {
  color: #1f5fa8;
  text-decoration: underline;
}
.retriv-docx-wrapper > section.retriv-docx a:hover {
  color: #164a83;
}
@media (prefers-reduced-motion: no-preference) {
  .retriv-docx-wrapper > section.retriv-docx {
    animation: docx-page-in 200ms ease-out both;
  }
}
@keyframes docx-page-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}
</style>
`
