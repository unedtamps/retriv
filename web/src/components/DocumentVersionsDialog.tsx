import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Check,
  Eye,
  GitBranch,
  Link2,
  LoaderCircle,
  Sparkles,
  Unlink2,
  X,
} from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import {
  docCategoryLabel,
  formatShortDate,
  isReadyStatus,
  STATUS_TONE_CLASSES,
} from '../lib/visuals'
import type {
  DocumentUpload,
  DocumentVersionsDetail,
  DocumentVersionSuggestion,
  VersionRelationKind,
  Workspace,
} from '../types'
import { Dialog } from './Dialog'

interface DocumentVersionsDialogProps {
  workspace: Workspace
  document: DocumentUpload
  workspaceDocuments: DocumentUpload[]
  canManage: boolean
  canView: boolean
  onOpenDocument?: (documentId: string) => void
  onChanged: () => void
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onClose: () => void
}

export function DocumentVersionsDialog({
  workspace,
  document,
  workspaceDocuments,
  canManage,
  canView,
  onOpenDocument,
  onChanged,
  onUnauthorized,
  onNotice,
  onClose,
}: DocumentVersionsDialogProps) {
  const [detail, setDetail] = useState<DocumentVersionsDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)

  // Manual link form state
  const [relatedDocId, setRelatedDocId] = useState('')
  const [direction, setDirection] = useState<'newer' | 'older'>('newer')
  const [relationKind, setRelationKind] = useState<VersionRelationKind>('supersedes')
  const [versionNote, setVersionNote] = useState('')

  // Per-suggestion confirmation overrides
  const [suggestionNewer, setSuggestionNewer] = useState<Record<string, string>>({})
  const [suggestionRelation, setSuggestionRelation] = useState<
    Record<string, VersionRelationKind>
  >({})

  const loadDetail = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true)
      setError(null)
      try {
        const result = await api.getDocumentVersions(workspace.id, document.id, signal)
        setDetail(result)
      } catch (caught) {
        if (caught instanceof Error && caught.name === 'AbortError') return
        if (isUnauthorized(caught)) onUnauthorized()
        else setError(getErrorMessage(caught))
      } finally {
        setLoading(false)
      }
    },
    [workspace.id, document.id, onUnauthorized],
  )

  useEffect(() => {
    const controller = new AbortController()
    api
      .getDocumentVersions(workspace.id, document.id, controller.signal)
      .then((result) => {
        setDetail(result)
        setError(null)
      })
      .catch((caught: unknown) => {
        if (caught instanceof Error && caught.name === 'AbortError') return
        if (isUnauthorized(caught)) onUnauthorized()
        else setError(getErrorMessage(caught))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [workspace.id, document.id, onUnauthorized])

  const linkableDocuments = useMemo(() => {
    const inGroup = new Set(detail?.group?.members.map((m) => m.document_id) ?? [])
    return workspaceDocuments.filter(
      (candidate) =>
        candidate.id !== document.id &&
        candidate.status !== 'deleting' &&
        !inGroup.has(candidate.id),
    )
  }, [workspaceDocuments, document.id, detail])

  async function handleRefreshProfile() {
    setBusyAction('enrich')
    try {
      const updated = await api.enrichDocument(workspace.id, document.id)
      setDetail(updated)
      onChanged()
      onNotice(`Document intelligence refreshed for ${document.original_filename}.`)
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setBusyAction(null)
    }
  }

  async function handleConfirmSuggestion(suggestion: DocumentVersionSuggestion) {
    setBusyAction(`confirm:${suggestion.id}`)
    try {
      const chosenNewer = suggestionNewer[suggestion.id] ?? suggestion.suggested_newer_document_id
      const chosenRelation = suggestionRelation[suggestion.id] ?? suggestion.suggested_relation
      await api.confirmVersionSuggestion(workspace.id, suggestion.id, {
        newer_document_id: chosenNewer,
        relation_kind: chosenRelation,
      })
      await loadDetail()
      onChanged()
      onNotice('Version link confirmed.')
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setBusyAction(null)
    }
  }

  async function handleDismissSuggestion(suggestion: DocumentVersionSuggestion) {
    setBusyAction(`dismiss:${suggestion.id}`)
    try {
      await api.dismissVersionSuggestion(workspace.id, suggestion.id)
      await loadDetail()
      onChanged()
      onNotice('Version suggestion dismissed.')
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setBusyAction(null)
    }
  }

  async function handleManualLink(event: React.FormEvent) {
    event.preventDefault()
    if (relatedDocId === '') return
    setBusyAction('link')
    try {
      const updated = await api.linkDocumentVersion(workspace.id, document.id, {
        related_document_id: relatedDocId,
        direction,
        relation_kind: relationKind,
        version_note: versionNote.trim() || undefined,
      })
      setDetail(updated)
      setRelatedDocId('')
      setVersionNote('')
      onChanged()
      onNotice('Document versions linked.')
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setBusyAction(null)
    }
  }

  async function handleUnlink() {
    setBusyAction('unlink')
    try {
      await api.unlinkDocumentVersion(workspace.id, document.id)
      await loadDetail()
      onChanged()
      onNotice(`${document.original_filename} removed from its version group.`)
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setBusyAction(null)
    }
  }

  async function handleToggleRelationKind(nextKind: VersionRelationKind) {
    if (detail?.group === null || detail?.group === undefined) return
    setBusyAction('relation')
    try {
      const updatedGroup = await api.updateVersionGroup(workspace.id, detail.group.group_id, {
        relation_kind: nextKind,
      })
      setDetail((prev) => (prev === null ? prev : { ...prev, group: updatedGroup }))
      onChanged()
      onNotice(
        nextKind === 'supersedes'
          ? 'Group set to Supersedes (older revisions excluded from default search).'
          : 'Group set to Periodic series (all periods remain searchable).',
      )
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setBusyAction(null)
    }
  }

  const profile = detail?.profile ?? document.profile ?? null
  const group = detail?.group ?? null
  const suggestions = detail?.pending_suggestions ?? []

  return (
    <Dialog
      open
      variant="drawer"
      title={document.original_filename}
      eyebrow={`${workspace.name} · Intelligence & versions`}
      onClose={onClose}
    >
      <div className="drawer-scroll space-y-6 px-5 py-5 sm:px-6">
        {error === null ? null : (
          <p className="error-note" role="alert">
            {error}
          </p>
        )}

        {loading && detail === null ? (
          <div className="flex items-center gap-2 py-8 text-sm text-ink-muted" role="status">
            <LoaderCircle aria-hidden="true" className="animate-spin" size={16} />
            Loading document intelligence…
          </div>
        ) : (
          <>
            {/* Extracted Intelligence */}
            <section className="rounded-card border border-line bg-surface p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="eyebrow">Extracted intelligence</p>
                  <h3 className="mt-1 text-sm font-semibold text-ink">
                    {profile?.title ?? document.original_filename}
                  </h3>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {profile !== null ? (
                    <span
                      className={`stamp ${
                        profile.status === 'enriched'
                          ? STATUS_TONE_CLASSES.success
                          : STATUS_TONE_CLASSES.neutral
                      }`}
                    >
                      {profile.status}
                    </span>
                  ) : null}
                  {canManage ? (
                    <button
                      type="button"
                      className="btn btn-secondary h-7 px-2.5 text-xs"
                      disabled={busyAction !== null || !isReadyStatus(document.status)}
                      title={
                        isReadyStatus(document.status)
                          ? 'Re-extract document intelligence and re-check version candidates'
                          : 'Wait for document extraction to finish'
                      }
                      onClick={() => void handleRefreshProfile()}
                    >
                      {busyAction === 'enrich' ? (
                        <LoaderCircle aria-hidden="true" className="animate-spin" size={13} />
                      ) : (
                        <Sparkles aria-hidden="true" size={13} />
                      )}
                      Refresh profile
                    </button>
                  ) : null}
                </div>
              </div>

              {profile === null ? (
                <p className="mt-3 text-xs text-ink-muted">
                  No intelligence profile extracted yet. Profiles are generated automatically when
                  document processing completes.
                </p>
              ) : (
                <div className="mt-3 space-y-3">
                  <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
                    <div>
                      <dt className="font-condensed text-[0.65rem] font-bold uppercase tracking-[0.08em] text-ink-faint">
                        Category
                      </dt>
                      <dd className="mt-0.5 text-ink">
                        {docCategoryLabel(profile.doc_category, profile.doc_subtype) || '—'}
                      </dd>
                    </div>
                    <div>
                      <dt className="font-condensed text-[0.65rem] font-bold uppercase tracking-[0.08em] text-ink-faint">
                        Period / Date
                      </dt>
                      <dd className="mt-0.5 font-mono text-ink">
                        {[profile.period_label, profile.effective_date].filter(Boolean).join(' · ') ||
                          '—'}
                      </dd>
                    </div>
                    <div>
                      <dt className="font-condensed text-[0.65rem] font-bold uppercase tracking-[0.08em] text-ink-faint">
                        Primary entities
                      </dt>
                      <dd className="mt-0.5 text-ink">
                        {profile.primary_entities.length > 0
                          ? profile.primary_entities.map((e) => e.name).join(', ')
                          : '—'}
                      </dd>
                    </div>
                    <div>
                      <dt className="font-condensed text-[0.65rem] font-bold uppercase tracking-[0.08em] text-ink-faint">
                        Ref / Version
                      </dt>
                      <dd className="mt-0.5 font-mono text-ink">
                        {[profile.document_number, profile.version_label]
                          .filter(Boolean)
                          .join(' · ') || '—'}
                      </dd>
                    </div>
                  </dl>
                  {profile.counterparties.length > 0 ? (
                    <p className="text-xs text-ink-muted">
                      <span className="font-medium text-ink">Counterparties:</span>{' '}
                      {profile.counterparties.map((c) => c.name).join(', ')}
                    </p>
                  ) : null}
                  {profile.summary ? (
                    <p className="text-xs leading-5 text-ink-muted">{profile.summary}</p>
                  ) : null}
                  {profile.topics.length > 0 ? (
                    <div className="flex flex-wrap gap-1">
                      {profile.topics.map((topic) => (
                        <span key={topic} className="stamp bg-sunken text-ink-muted">
                          {topic}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              )}
            </section>

            {/* Pending Version Suggestions */}
            {suggestions.length > 0 ? (
              <section className="rounded-card border border-line bg-surface p-4">
                <div className="flex items-center justify-between gap-2">
                  <p className="eyebrow">Pending version suggestions</p>
                  <span className={`stamp ${STATUS_TONE_CLASSES.info}`}>
                    {suggestions.length} pending
                  </span>
                </div>
                <div className="mt-3 space-y-3">
                  {suggestions.map((suggestion) => {
                    const other =
                      suggestion.document_a.document_id === document.id
                        ? suggestion.document_b
                        : suggestion.document_a
                    const chosenNewer =
                      suggestionNewer[suggestion.id] ?? suggestion.suggested_newer_document_id
                    const chosenRelation =
                      suggestionRelation[suggestion.id] ?? suggestion.suggested_relation
                    return (
                      <div
                        key={suggestion.id}
                        className="rounded-control border border-line bg-sunken/50 p-3"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <p className="truncate text-xs font-semibold text-ink">
                              {other.filename}
                            </p>
                            <p className="mt-0.5 text-[0.7rem] text-ink-muted">
                              {suggestion.reasons.join(' · ')}
                            </p>
                          </div>
                          <span className="stamp shrink-0 bg-surface text-ink-muted">
                            {Math.round(suggestion.confidence * 100)}% match
                          </span>
                        </div>

                        {canManage ? (
                          <div className="mt-3 space-y-2">
                            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                              <label className="text-[0.7rem] text-ink-muted">
                                Newer revision
                                <select
                                  className="field mt-1 py-1 text-xs"
                                  value={chosenNewer}
                                  onChange={(e) =>
                                    setSuggestionNewer((prev) => ({
                                      ...prev,
                                      [suggestion.id]: e.target.value,
                                    }))
                                  }
                                >
                                  <option value={suggestion.document_a.document_id}>
                                    {suggestion.document_a.filename}
                                  </option>
                                  <option value={suggestion.document_b.document_id}>
                                    {suggestion.document_b.filename}
                                  </option>
                                </select>
                              </label>
                              <label className="text-[0.7rem] text-ink-muted">
                                Relationship
                                <select
                                  className="field mt-1 py-1 text-xs"
                                  value={chosenRelation}
                                  onChange={(e) =>
                                    setSuggestionRelation((prev) => ({
                                      ...prev,
                                      [suggestion.id]: e.target.value as VersionRelationKind,
                                    }))
                                  }
                                >
                                  <option value="supersedes">Supersedes (replaces older)</option>
                                  <option value="periodic_series">
                                    Periodic series (keep all searchable)
                                  </option>
                                </select>
                              </label>
                            </div>
                            <div className="flex items-center justify-end gap-2">
                              <button
                                type="button"
                                className="btn btn-secondary h-7 px-2.5 text-xs"
                                disabled={busyAction !== null}
                                onClick={() => void handleDismissSuggestion(suggestion)}
                              >
                                <X aria-hidden="true" size={13} />
                                Dismiss
                              </button>
                              <button
                                type="button"
                                className="btn btn-primary h-7 px-2.5 text-xs"
                                disabled={busyAction !== null}
                                onClick={() => void handleConfirmSuggestion(suggestion)}
                              >
                                <Check aria-hidden="true" size={13} />
                                Confirm link
                              </button>
                            </div>
                          </div>
                        ) : (
                          <p className="mt-2 text-[0.7rem] text-ink-faint">
                            A workspace editor or owner can confirm or dismiss this suggestion.
                          </p>
                        )}
                      </div>
                    )
                  })}
                </div>
              </section>
            ) : null}

            {/* Confirmed Version History */}
            <section className="rounded-card border border-line bg-surface p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <p className="eyebrow">Confirmed version history</p>
                  <h3 className="mt-1 flex items-center gap-1.5 text-sm font-semibold text-ink">
                    <GitBranch aria-hidden="true" size={14} className="text-ink-faint" />
                    {group !== null ? group.name : 'Standalone document'}
                  </h3>
                </div>
                {group !== null && canManage ? (
                  <button
                    type="button"
                    className="btn btn-secondary h-7 px-2.5 text-xs text-danger"
                    disabled={busyAction !== null}
                    onClick={() => void handleUnlink()}
                  >
                    <Unlink2 aria-hidden="true" size={13} />
                    Unlink
                  </button>
                ) : null}
              </div>

              {group === null ? (
                <p className="mt-2 text-xs text-ink-muted">
                  Not linked to any other version in this workspace.
                </p>
              ) : (
                <div className="mt-3 space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-2 rounded-control border border-line bg-sunken/40 px-3 py-2">
                    <div className="text-xs text-ink-muted">
                      Mode:{' '}
                      <span className="font-medium text-ink">
                        {group.relation_kind === 'supersedes'
                          ? 'Supersedes (older versions excluded from default search)'
                          : 'Periodic series (every period remains searchable)'}
                      </span>
                    </div>
                    {canManage ? (
                      <div className="flex items-center gap-1">
                        <button
                          type="button"
                          className={`rounded-stamp border px-2 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wider ${
                            group.relation_kind === 'supersedes'
                              ? 'border-accent bg-accent/10 text-ink'
                              : 'border-line bg-surface text-ink-muted hover:text-ink'
                          }`}
                          disabled={busyAction !== null || group.relation_kind === 'supersedes'}
                          onClick={() => void handleToggleRelationKind('supersedes')}
                        >
                          Supersedes
                        </button>
                        <button
                          type="button"
                          className={`rounded-stamp border px-2 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wider ${
                            group.relation_kind === 'periodic_series'
                              ? 'border-accent bg-accent/10 text-ink'
                              : 'border-line bg-surface text-ink-muted hover:text-ink'
                          }`}
                          disabled={
                            busyAction !== null || group.relation_kind === 'periodic_series'
                          }
                          onClick={() => void handleToggleRelationKind('periodic_series')}
                        >
                          Periodic series
                        </button>
                      </div>
                    ) : null}
                  </div>

                  <ol className="divide-y divide-line rounded-control border border-line">
                    {[...group.members].reverse().map((member) => {
                      const isCurrent = member.document_id === document.id
                      const isSuperseded =
                        group.relation_kind === 'supersedes' && !member.is_latest
                      return (
                        <li
                          key={member.document_id}
                          className={`flex items-center justify-between gap-3 px-3 py-2.5 ${
                            isCurrent ? 'bg-sunken/50' : ''
                          }`}
                        >
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <span className="font-mono text-[0.65rem] text-ink-faint">
                                v{member.version_rank}
                              </span>
                              <span className="truncate text-xs font-semibold text-ink">
                                {member.filename}
                              </span>
                              {member.is_latest ? (
                                <span className={`stamp ${STATUS_TONE_CLASSES.success}`}>
                                  Latest
                                </span>
                              ) : isSuperseded ? (
                                <span className={`stamp ${STATUS_TONE_CLASSES.warning}`}>
                                  Superseded
                                </span>
                              ) : (
                                <span className={`stamp ${STATUS_TONE_CLASSES.neutral}`}>
                                  Period
                                </span>
                              )}
                              {isCurrent ? (
                                <span className="font-mono text-[0.62rem] text-ink-faint">
                                  (current)
                                </span>
                              ) : null}
                            </div>
                            <p className="mt-0.5 font-mono text-[0.65rem] text-ink-muted">
                              {[
                                member.period_label,
                                member.effective_date,
                                member.version_note,
                                formatShortDate(member.created_at),
                              ]
                                .filter(Boolean)
                                .join(' · ')}
                            </p>
                          </div>
                          {canView && !isCurrent && onOpenDocument !== undefined ? (
                            <button
                              type="button"
                              className="btn-icon shrink-0"
                              aria-label={`Preview ${member.filename}`}
                              onClick={() => onOpenDocument(member.document_id)}
                            >
                              <Eye aria-hidden="true" size={14} />
                            </button>
                          ) : null}
                        </li>
                      )
                    })}
                  </ol>
                </div>
              )}
            </section>

            {/* Manual Link Form */}
            {canManage && linkableDocuments.length > 0 ? (
              <section className="rounded-card border border-line bg-surface p-4">
                <p className="eyebrow">Manual link</p>
                <p className="mt-1 text-xs text-ink-muted">
                  Link another document in {workspace.name} as a previous or newer revision of this
                  file.
                </p>
                <form className="mt-3 space-y-3" onSubmit={(e) => void handleManualLink(e)}>
                  <label className="block text-xs text-ink-muted">
                    Related document
                    <select
                      className="field mt-1 text-xs"
                      value={relatedDocId}
                      onChange={(e) => setRelatedDocId(e.target.value)}
                      required
                    >
                      <option value="">Select a document…</option>
                      {linkableDocuments.map((candidate) => (
                        <option key={candidate.id} value={candidate.id}>
                          {candidate.original_filename}
                          {candidate.profile?.period_label
                            ? ` (${candidate.profile.period_label})`
                            : ''}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <label className="block text-xs text-ink-muted">
                      This document is
                      <select
                        className="field mt-1 text-xs"
                        value={direction}
                        onChange={(e) => setDirection(e.target.value as 'newer' | 'older')}
                      >
                        <option value="newer">Newer than selected</option>
                        <option value="older">Older than selected</option>
                      </select>
                    </label>
                    <label className="block text-xs text-ink-muted">
                      Relationship
                      <select
                        className="field mt-1 text-xs"
                        value={relationKind}
                        onChange={(e) => setRelationKind(e.target.value as VersionRelationKind)}
                      >
                        <option value="supersedes">Supersedes</option>
                        <option value="periodic_series">Periodic series</option>
                      </select>
                    </label>
                  </div>
                  <label className="block text-xs text-ink-muted">
                    Version note (optional)
                    <input
                      type="text"
                      className="field mt-1 text-xs"
                      placeholder="e.g. Updated leave quota for 2026"
                      maxLength={255}
                      value={versionNote}
                      onChange={(e) => setVersionNote(e.target.value)}
                    />
                  </label>
                  <div className="flex justify-end">
                    <button
                      type="submit"
                      className="btn btn-primary h-8 px-3 text-xs"
                      disabled={busyAction !== null || relatedDocId === ''}
                    >
                      <Link2 aria-hidden="true" size={14} />
                      Link version
                    </button>
                  </div>
                </form>
              </section>
            ) : null}
          </>
        )}
      </div>
    </Dialog>
  )
}
