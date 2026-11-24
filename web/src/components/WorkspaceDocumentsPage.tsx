import { useDeferredValue, useEffect, useState } from 'react'
import { Check, ChevronDown, ChevronUp, FileUp, GitBranch, LockKeyhole, Search, X } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { ApiResource } from '../hooks/useApiResource'
import type { ProcessingRuns } from '../hooks/useProcessingRuns'
import type { WorkspaceDocumentsData } from '../hooks/useWorkspaceDocuments'
import {
  docCategoryLabel,
  documentType,
  isActiveStatus,
  isFailedStatus,
  isReadyStatus,
  STATUS_TONE_CLASSES,
} from '../lib/visuals'
import type { DocumentUpload, DocumentVersionSuggestion, Workspace } from '../types'
import { DocumentTable } from './DocumentTable'
import { DocumentVersionsDialog } from './DocumentVersionsDialog'
import { RefreshButton } from './ui/RefreshButton'

type StatusFilter = 'all' | 'ready' | 'active' | 'failed'

const TYPE_CHIPS = ['PDF', 'DOCX', 'XLSX', 'CSV', 'JSON', 'PPTX'] as const

interface WorkspaceDocumentsPageProps {
  workspace: Workspace
  documents: ApiResource<WorkspaceDocumentsData>
  runs?: ProcessingRuns
  focusDocumentId?: string | null
  canReindex?: boolean
  reindexingId?: string | null
  onReindex?: (document: DocumentUpload) => void
  canView?: boolean
  onView?: (document: DocumentUpload) => void
  onOpenDocumentById?: (documentId: string) => void
  onUnauthorized?: () => void
  onNotice?: (message: string, tone?: 'success' | 'error') => void
  onUpload: () => void
  onDeleteDocument: (document: DocumentUpload) => void
}

function matchesSearchQuery(document: DocumentUpload, query: string): boolean {
  if (document.original_filename.toLowerCase().includes(query)) return true
  const profile = document.profile
  if (profile === undefined || profile === null) return false
  if (profile.title?.toLowerCase().includes(query)) return true
  if (profile.doc_category?.toLowerCase().includes(query)) return true
  if (profile.doc_subtype?.toLowerCase().includes(query)) return true
  if (docCategoryLabel(profile.doc_category, profile.doc_subtype).toLowerCase().includes(query)) {
    return true
  }
  if (profile.period_label?.toLowerCase().includes(query)) return true
  if (profile.document_number?.toLowerCase().includes(query)) return true
  if (profile.primary_entities.some((entity) => entity.name.toLowerCase().includes(query))) {
    return true
  }
  return false
}

export function WorkspaceDocumentsPage({
  workspace,
  documents,
  runs,
  focusDocumentId,
  canReindex,
  reindexingId,
  onReindex,
  canView,
  onView,
  onOpenDocumentById,
  onUnauthorized,
  onNotice,
  onUpload,
  onDeleteDocument,
}: WorkspaceDocumentsPageProps) {
  const items = documents.data?.items ?? []
  const total = documents.data?.total ?? 0
  const restricted = workspace.membership_role === null

  const [nameFilter, setNameFilter] = useState('')
  const [typeFilter, setTypeFilter] = useState<(typeof TYPE_CHIPS)[number] | 'all'>('all')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [hideSuperseded, setHideSuperseded] = useState(false)
  const [inspectDocumentId, setInspectDocumentId] = useState<string | null>(null)
  const [suggestions, setSuggestions] = useState<DocumentVersionSuggestion[]>([])
  const [suggestionsExpanded, setSuggestionsExpanded] = useState(false)
  const [suggestionsTick, setSuggestionsTick] = useState(0)
  const [busySuggestionId, setBusySuggestionId] = useState<string | null>(null)
  const deferredName = useDeferredValue(nameFilter.trim().toLowerCase())

  useEffect(() => {
    setTypeFilter('all')
    setStatusFilter('all')
    setNameFilter('')
    setHideSuperseded(false)
    setInspectDocumentId(null)
    setSuggestions([])
    setSuggestionsExpanded(false)
  }, [workspace.id])

  useEffect(() => {
    if (restricted) return
    const controller = new AbortController()
    api
      .listVersionSuggestions(workspace.id, { status: 'pending', signal: controller.signal })
      .then((result) => {
        setSuggestions(result.items)
      })
      .catch((caught: unknown) => {
        if (caught instanceof Error && caught.name === 'AbortError') return
        if (isUnauthorized(caught)) onUnauthorized?.()
      })
    return () => controller.abort()
  }, [workspace.id, restricted, documents.data, suggestionsTick, onUnauthorized])

  const visibleSuggestions = restricted ? [] : suggestions

  const supersededCount = items.filter((doc) => doc.version?.is_superseded === true).length
  const inspectDocument =
    inspectDocumentId === null
      ? null
      : (items.find((item) => item.id === inspectDocumentId) ?? null)

  const filtered = items.filter((document) => {
    if (hideSuperseded && document.version?.is_superseded) return false
    if (typeFilter !== 'all') {
      const type = documentType(document.original_filename, document.detected_media_type)
      if (type !== typeFilter) return false
    }
    if (statusFilter === 'ready' && !isReadyStatus(document.status)) return false
    if (statusFilter === 'active' && !isActiveStatus(document.status)) return false
    if (statusFilter === 'failed' && !isFailedStatus(document.status)) return false
    if (deferredName !== '' && !matchesSearchQuery(document, deferredName)) {
      return false
    }
    return true
  })

  async function handleQuickConfirm(suggestion: DocumentVersionSuggestion) {
    setBusySuggestionId(suggestion.id)
    try {
      await api.confirmVersionSuggestion(workspace.id, suggestion.id, {
        newer_document_id: suggestion.suggested_newer_document_id,
        relation_kind: suggestion.suggested_relation,
      })
      setSuggestions((prev) => prev.filter((item) => item.id !== suggestion.id))
      documents.reload()
      setSuggestionsTick((tick) => tick + 1)
      onNotice?.('Version link confirmed.')
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized?.()
      else onNotice?.(getErrorMessage(caught), 'error')
    } finally {
      setBusySuggestionId(null)
    }
  }

  async function handleQuickDismiss(suggestion: DocumentVersionSuggestion) {
    setBusySuggestionId(suggestion.id)
    try {
      await api.dismissVersionSuggestion(workspace.id, suggestion.id)
      setSuggestions((prev) => prev.filter((item) => item.id !== suggestion.id))
      documents.reload()
      setSuggestionsTick((tick) => tick + 1)
      onNotice?.('Version suggestion dismissed.')
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized?.()
      else onNotice?.(getErrorMessage(caught), 'error')
    } finally {
      setBusySuggestionId(null)
    }
  }

  function handleRefreshAll() {
    documents.reload()
    setSuggestionsTick((tick) => tick + 1)
  }

  return (
    <div className="mx-auto max-w-[92rem] px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-2.5">
          <h1 className="font-condensed text-xl font-bold uppercase tracking-[0.05em] text-ink">
            Documents
          </h1>
          <span className="stamp">
            {restricted ? total : `${filtered.length} of ${total}`}
          </span>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {restricted ? null : (
            <RefreshButton
              label="Refresh document status"
              loading={documents.loading}
              onRefresh={handleRefreshAll}
            />
          )}
          {workspace.can_upload ? (
            <button type="button" className="btn btn-primary shrink-0" onClick={onUpload}>
              <FileUp aria-hidden="true" size={16} />
              Upload document
            </button>
          ) : null}
        </div>
      </header>

      {restricted ? (
        <section className="mt-6 rounded-card border border-dashed border-line-strong bg-surface px-6 py-14 text-center">
          <LockKeyhole aria-hidden="true" className="mx-auto text-ink-faint" size={26} />
          <h2 className="mt-3 text-sm font-semibold text-ink">Documents are members-only</h2>
          <p className="mx-auto mt-1.5 max-w-sm text-sm leading-6 text-ink-muted">
            Ask a workspace owner for viewer, editor or owner access. A platform admin role alone
            does not grant document access.
          </p>
        </section>
      ) : (
        <>
          {visibleSuggestions.length > 0 ? (
            <section
              aria-label="Pending version suggestions"
              className="mt-5 rounded-card border border-line bg-surface px-4 py-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2.5">
                  <GitBranch aria-hidden="true" size={15} className="shrink-0 text-blue" />
                  <p className="text-xs text-ink">
                    <span className="font-semibold">
                      {visibleSuggestions.length === 1
                        ? '1 potential version match detected'
                        : `${visibleSuggestions.length} potential version matches detected`}
                    </span>
                    <span className="text-ink-muted">
                      {' · '}Review and confirm before linking revisions.
                    </span>
                  </p>
                </div>
                <button
                  type="button"
                  className="btn btn-secondary h-7 shrink-0 px-2.5 text-xs"
                  aria-expanded={suggestionsExpanded}
                  onClick={() => setSuggestionsExpanded((open) => !open)}
                >
                  {suggestionsExpanded ? (
                    <>
                      <ChevronUp aria-hidden="true" size={13} />
                      Hide
                    </>
                  ) : (
                    <>
                      <ChevronDown aria-hidden="true" size={13} />
                      Review
                    </>
                  )}
                </button>
              </div>

              {suggestionsExpanded ? (
                <ul className="mt-3 divide-y divide-line border-t border-line pt-2">
                  {visibleSuggestions.map((suggestion) => {
                    const newerDoc =
                      suggestion.suggested_newer_document_id === suggestion.document_a.document_id
                        ? suggestion.document_a
                        : suggestion.document_b
                    const olderDoc =
                      newerDoc.document_id === suggestion.document_a.document_id
                        ? suggestion.document_b
                        : suggestion.document_a
                    const busy = busySuggestionId === suggestion.id
                    return (
                      <li
                        key={suggestion.id}
                        className="flex flex-col gap-2 py-2.5 sm:flex-row sm:items-center sm:justify-between"
                      >
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-1.5 text-xs">
                            <span className="truncate font-semibold text-ink">
                              {newerDoc.filename}
                            </span>
                            <span className="text-ink-faint">
                              {suggestion.suggested_relation === 'supersedes'
                                ? 'supersedes'
                                : 'series with'}
                            </span>
                            <span className="truncate font-medium text-ink-muted">
                              {olderDoc.filename}
                            </span>
                            <span className={`stamp ${STATUS_TONE_CLASSES.info}`}>
                              {Math.round(suggestion.confidence * 100)}%
                            </span>
                          </div>
                          {suggestion.reasons.length > 0 ? (
                            <p className="mt-0.5 text-[0.7rem] text-ink-muted">
                              {suggestion.reasons.join(' · ')}
                            </p>
                          ) : null}
                        </div>
                        <div className="flex shrink-0 items-center gap-1.5">
                          <button
                            type="button"
                            className="btn btn-secondary h-7 px-2.5 text-xs"
                            disabled={busy}
                            onClick={() => setInspectDocumentId(newerDoc.document_id)}
                          >
                            Details
                          </button>
                          {workspace.can_delete ? (
                            <>
                              <button
                                type="button"
                                className="btn btn-secondary h-7 px-2.5 text-xs"
                                disabled={busy}
                                onClick={() => void handleQuickDismiss(suggestion)}
                              >
                                <X aria-hidden="true" size={13} />
                                Dismiss
                              </button>
                              <button
                                type="button"
                                className="btn btn-primary h-7 px-2.5 text-xs"
                                disabled={busy}
                                onClick={() => void handleQuickConfirm(suggestion)}
                              >
                                <Check aria-hidden="true" size={13} />
                                Confirm
                              </button>
                            </>
                          ) : null}
                        </div>
                      </li>
                    )
                  })}
                </ul>
              ) : null}
            </section>
          ) : null}

          <div className="relative mt-5">
            <Search
              aria-hidden="true"
              className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-ink-faint"
              size={15}
            />
            <input
              type="search"
              className="field pl-9"
              placeholder="Filter by name, entity, type, or period"
              aria-label="Filter documents by name, entity, type, or period"
              value={nameFilter}
              onChange={(event) => setNameFilter(event.target.value)}
            />
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by type">
              <button
                type="button"
                className={typeFilter === 'all' ? 'nav-link-active px-3 py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em]' : 'rounded-control border border-line bg-surface px-3 py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted transition-colors hover:border-line-strong hover:text-ink'}
                aria-pressed={typeFilter === 'all'}
                onClick={() => setTypeFilter('all')}
              >
                All
              </button>
              {TYPE_CHIPS.map((type) => (
                <button
                  key={type}
                  type="button"
                  className={typeFilter === type ? 'nav-link-active px-3 py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em]' : 'rounded-control border border-line bg-surface px-3 py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted transition-colors hover:border-line-strong hover:text-ink'}
                  aria-pressed={typeFilter === type}
                  onClick={() => setTypeFilter(type)}
                >
                  {type}
                </button>
              ))}
            </div>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              {supersededCount > 0 ? (
                <button
                  type="button"
                  className={
                    hideSuperseded
                      ? 'nav-link-active px-3 py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em]'
                      : 'rounded-control border border-line bg-surface px-3 py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted transition-colors hover:border-line-strong hover:text-ink'
                  }
                  aria-pressed={hideSuperseded}
                  onClick={() => setHideSuperseded((current) => !current)}
                >
                  Hide superseded ({supersededCount})
                </button>
              ) : null}
              <select
                className="field w-auto py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted"
                aria-label="Filter by status"
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value as StatusFilter)}
              >
                <option value="all">Status: all</option>
                <option value="ready">Status: ready</option>
                <option value="active">Status: processing</option>
                <option value="failed">Status: failed</option>
              </select>
            </div>
          </div>

          <div className="mt-3">
            <DocumentTable
              documents={filtered}
              total={total}
              loading={documents.loading}
              error={documents.error}
              canDelete={workspace.can_delete}
              canReindex={canReindex}
              reindexingId={reindexingId}
              onReindex={onReindex}
              canView={canView}
              onView={onView}
              onInspectVersions={(document) => setInspectDocumentId(document.id)}
              runs={runs}
              focusDocumentId={focusDocumentId}
              onDelete={onDeleteDocument}
            />
          </div>
        </>
      )}

      {inspectDocument !== null ? (
        <DocumentVersionsDialog
          workspace={workspace}
          document={inspectDocument}
          workspaceDocuments={items}
          canManage={workspace.can_delete}
          canView={Boolean(canView)}
          onOpenDocument={(documentId) => {
            setInspectDocumentId(null)
            onOpenDocumentById?.(documentId)
          }}
          onChanged={handleRefreshAll}
          onUnauthorized={onUnauthorized ?? (() => {})}
          onNotice={onNotice ?? (() => {})}
          onClose={() => setInspectDocumentId(null)}
        />
      ) : null}
    </div>
  )
}
