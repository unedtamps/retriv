import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import {
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  FileText,
  FileUp,
  LoaderCircle,
  LockKeyhole,
  ShieldCheck,
} from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { DocumentUpload, Workspace } from '../types'
import { Panel, PanelHeader } from './ui/Panel'

interface UploadContextProps {
  token: string
  workspaces: Workspace[]
  onUnauthorized: () => void
}

interface UploadConfirmation {
  document: DocumentUpload
  workspaceName: string
}

const supportedFile = /\.(pdf|docx|csv)$/i

export function UploadContext({ token, workspaces, onUnauthorized }: UploadContextProps) {
  const availableWorkspaces = workspaces.filter((workspace) => workspace.can_upload)
  const [workspaceId, setWorkspaceId] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmation, setConfirmation] = useState<UploadConfirmation | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const uploadController = useRef<AbortController | null>(null)
  const uploadInProgress = useRef(false)

  useEffect(() => () => uploadController.current?.abort(), [])

  const selectedWorkspace = availableWorkspaces.find((workspace) => workspace.id === workspaceId)

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    const chosen = event.target.files?.[0] ?? null
    setFile(chosen)
    setError(
      chosen !== null && !supportedFile.test(chosen.name)
        ? 'Only PDF, DOCX and CSV files can be processed as context.'
        : chosen !== null && chosen.size === 0
          ? 'Choose a non-empty file to upload.'
          : null,
    )
  }

  async function submitUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (uploadInProgress.current) return
    if (selectedWorkspace === undefined) {
      setError('Choose a workspace where you can upload documents.')
      return
    }
    if (file === null) {
      setError('Choose a PDF, DOCX or CSV file to upload.')
      return
    }
    if (!supportedFile.test(file.name)) {
      setError('Only PDF, DOCX and CSV files can be processed as context.')
      return
    }
    if (file.size === 0) {
      setError('Choose a non-empty file to upload.')
      return
    }

    uploadInProgress.current = true
    setUploading(true)
    setError(null)
    setConfirmation(null)
    const controller = new AbortController()
    uploadController.current = controller
    try {
      const document = await api.uploadDocument(token, selectedWorkspace.id, file, controller.signal)
      setConfirmation({ document, workspaceName: selectedWorkspace.name })
      setFile(null)
      if (fileInput.current) fileInput.current.value = ''
    } catch (caught) {
      if (caught instanceof Error && caught.name === 'AbortError') return
      if (isUnauthorized(caught)) onUnauthorized()
      else setError(getErrorMessage(caught))
    } finally {
      uploadController.current = null
      uploadInProgress.current = false
      setUploading(false)
    }
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
      <header className="border-b border-line pb-6">
        <p className="eyebrow">Document intake</p>
        <h1 className="mt-2 text-2xl font-semibold tracking-[-0.02em] text-ink">Upload context</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-ink-muted">
          Add a document to a workspace. Once accepted, its content is extracted and indexed in
          the background.
        </p>
      </header>

      {availableWorkspaces.length === 0 ? (
        <Panel className="mt-6">
          <div className="px-6 py-12 text-center">
            <LockKeyhole
              className="mx-auto text-ink-faint"
              aria-hidden="true"
              size={28}
            />
            <h2 className="mt-4 text-lg font-semibold text-ink">
              No workspaces available for upload
            </h2>
            <p className="mx-auto mt-2 max-w-lg text-sm leading-6 text-ink-muted">
              Ask a workspace owner for owner or editor access. A platform admin role alone does
              not grant document access.
            </p>
            <a href="/dashboard" className="btn btn-secondary mt-6">
              View workspaces
              <ArrowRight aria-hidden="true" size={16} />
            </a>
          </div>
        </Panel>
      ) : (
        <div className="mt-6 grid gap-5 lg:grid-cols-[minmax(0,1fr)_18rem] lg:items-start">
          <Panel>
            <PanelHeader eyebrow="New document" title="Select its destination and file" />
            <form className="space-y-6 px-5 py-6" onSubmit={submitUpload}>
              <div>
                <label htmlFor="upload-workspace" className="label">
                  Workspace
                </label>
                <select
                  id="upload-workspace"
                  className="field"
                  value={workspaceId}
                  disabled={uploading}
                  onChange={(event) => {
                    setWorkspaceId(event.target.value)
                    setError(null)
                  }}
                >
                  <option value="">Choose a workspace</option>
                  {availableWorkspaces.map((workspace) => (
                    <option key={workspace.id} value={workspace.id}>
                      {workspace.name} · {workspace.slug}
                    </option>
                  ))}
                </select>
                {selectedWorkspace === undefined ? (
                  <p className="mt-1.5 text-xs leading-5 text-ink-muted">
                    Only workspaces where you are an owner or editor are shown.
                  </p>
                ) : (
                  <p className="mt-1.5 text-xs leading-5 text-ink-muted">
                    {selectedWorkspace.description ?? selectedWorkspace.slug} · Your role:{' '}
                    {selectedWorkspace.membership_role}
                  </p>
                )}
              </div>

              <div>
                <label htmlFor="upload-file" className="label">
                  Document
                </label>
                <input
                  id="upload-file"
                  ref={fileInput}
                  className="field cursor-pointer text-sm file:mr-3 file:cursor-pointer file:border-0 file:border-r file:border-line file:bg-sunken file:px-3 file:py-1 file:text-sm file:font-medium file:text-ink"
                  type="file"
                  accept=".pdf,.docx,.csv,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/csv"
                  aria-describedby="upload-file-help"
                  disabled={uploading}
                  onChange={chooseFile}
                />
                <p id="upload-file-help" className="mt-1.5 text-xs leading-5 text-ink-muted">
                  PDF, DOCX or CSV. The server validates the file and enforces its size limit. A
                  CSV is stored as rows and queried with SQL rather than indexed for meaning, so
                  the agent can count, total and group its lines but not search their wording.
                </p>
                {file === null ? null : (
                  <div className="mt-3 flex items-center gap-3 rounded-card border border-line bg-sunken px-3.5 py-2.5 text-sm">
                    <FileText aria-hidden="true" className="shrink-0 text-accent-ink" size={18} />
                    <span className="min-w-0 flex-1 truncate text-ink">{file.name}</span>
                    <span className="shrink-0 font-mono text-meta text-ink-muted">
                      {formatBytes(file.size)}
                    </span>
                  </div>
                )}
              </div>

              {error === null ? null : (
                <p className="error-note" role="alert">
                  <CircleAlert aria-hidden="true" className="mt-0.5 shrink-0" size={16} />
                  {error}
                </p>
              )}

              <div className="flex flex-wrap items-center gap-4 border-t border-line pt-5">
                <button
                  type="submit"
                  className="btn btn-primary min-w-40"
                  disabled={
                    uploading ||
                    file === null ||
                    file.size === 0 ||
                    !supportedFile.test(file.name) ||
                    selectedWorkspace === undefined
                  }
                >
                  {uploading ? (
                    <LoaderCircle aria-hidden="true" className="animate-spin" size={17} />
                  ) : (
                    <FileUp aria-hidden="true" size={17} />
                  )}
                  {uploading ? 'Uploading…' : 'Upload document'}
                </button>
                <p className="text-xs text-ink-muted">Context indexing continues after upload.</p>
              </div>
            </form>
          </Panel>

          <aside className="rounded-card border border-line bg-surface p-5" aria-label="Upload policy">
            <ShieldCheck aria-hidden="true" className="text-accent-ink" size={22} />
            <h2 className="mt-3 text-sm font-semibold text-ink">Workspace-first access</h2>
            <p className="mt-1.5 text-sm leading-6 text-ink-muted">
              Every document belongs to the workspace you choose. Membership is checked again by
              the API when you submit.
            </p>
            <p className="mt-4 border-t border-line pt-3 font-mono text-meta text-ink-faint">
              {availableWorkspaces.length} available workspace
              {availableWorkspaces.length === 1 ? '' : 's'}
            </p>
          </aside>
        </div>
      )}

      {confirmation === null ? null : (
        <section
          className="mt-5 rounded-card border border-success/30 bg-success-soft px-5 py-4"
          role="status"
          aria-live="polite"
          aria-labelledby="upload-result-title"
        >
          <div className="flex items-start gap-3">
            <CheckCircle2
              aria-hidden="true"
              className="mt-0.5 shrink-0 text-success"
              size={20}
            />
            <div className="min-w-0">
              <h2 id="upload-result-title" className="text-sm font-semibold text-success">
                {confirmation.document.duplicate ? 'Document already exists' : 'Document accepted'}
              </h2>
              <p className="mt-1 text-sm leading-6 text-success">
                {confirmation.document.duplicate
                  ? 'An identical document already exists in this workspace; no new upload was created.'
                  : 'The file is stored. Extraction and indexing happen asynchronously.'}
              </p>
              <p className="mt-2.5 break-all font-mono text-meta text-success">
                {confirmation.document.original_filename} · {confirmation.workspaceName} · ID{' '}
                {confirmation.document.id}
              </p>
            </div>
          </div>
        </section>
      )}
    </div>
  )
}

function formatBytes(size: number): string {
  return size < 1024 * 1024
    ? `${Math.max(1, Math.ceil(size / 1024))} KB`
    : `${(size / (1024 * 1024)).toFixed(1)} MB`
}
