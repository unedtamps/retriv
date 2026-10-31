import { useEffect, useId, useRef, useState, type ChangeEvent, type DragEvent, type FormEvent } from 'react'
import {
  CheckCircle2,
  CircleAlert,
  FileText,
  FileUp,
  LoaderCircle,
  UploadCloud,
} from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import { formatBytes } from '../lib/visuals'
import type { DocumentUpload, Workspace } from '../types'

interface UploadFormProps {
  token: string
  workspaces: Workspace[]
  initialWorkspaceId?: string
  lockWorkspace?: boolean
  onUnauthorized: () => void
  onUploaded?: (document: DocumentUpload, workspace: Workspace) => void
}

interface UploadConfirmation {
  document: DocumentUpload
  workspaceName: string
}

const supportedFile = /\.(pdf|docx|csv)$/i

export function UploadForm({
  token,
  workspaces,
  initialWorkspaceId,
  lockWorkspace = false,
  onUnauthorized,
  onUploaded,
}: UploadFormProps) {
  const availableWorkspaces = workspaces.filter((workspace) => workspace.can_upload)
  const formId = useId()
  const [workspaceId, setWorkspaceId] = useState(initialWorkspaceId ?? '')
  const [file, setFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmation, setConfirmation] = useState<UploadConfirmation | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const uploadController = useRef<AbortController | null>(null)
  const uploadInProgress = useRef(false)
  const [followedWorkspaceId, setFollowedWorkspaceId] = useState(initialWorkspaceId)

  useEffect(() => () => uploadController.current?.abort(), [])

  if (initialWorkspaceId !== followedWorkspaceId) {
    setFollowedWorkspaceId(initialWorkspaceId)
    if (!lockWorkspace && initialWorkspaceId !== undefined) setWorkspaceId(initialWorkspaceId)
  }

  const selectedWorkspace = availableWorkspaces.find((workspace) => workspace.id === workspaceId)

  function inspectFile(chosen: File | null) {
    setFile(chosen)
    setConfirmation(null)
    setError(
      chosen !== null && !supportedFile.test(chosen.name)
        ? 'Only PDF, DOCX and CSV files can be processed as context.'
        : chosen !== null && chosen.size === 0
          ? 'Choose a non-empty file to upload.'
          : null,
    )
  }

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    inspectFile(event.target.files?.[0] ?? null)
  }

  function dropFile(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    inspectFile(event.dataTransfer.files?.[0] ?? null)
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
      onUploaded?.(document, selectedWorkspace)
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

  const workspaceFieldId = `${formId}-workspace`
  const fileFieldId = `${formId}-file`
  const helpId = `${formId}-file-help`

  return (
    <div>
      <form className="space-y-5" onSubmit={submitUpload}>
        {lockWorkspace ? (
          <div>
            <p className="label">Workspace</p>
            {selectedWorkspace === undefined ? (
              <p className="error-note" role="alert">
                You cannot upload to this workspace.
              </p>
            ) : (
              <div className="flex items-center justify-between gap-3 rounded-card border border-line bg-sunken/70 px-3.5 py-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-ink">{selectedWorkspace.name}</p>
                  <p className="mt-0.5 font-mono text-meta text-ink-faint">
                    {selectedWorkspace.slug}
                  </p>
                </div>
                <span className="badge shrink-0">{selectedWorkspace.membership_role}</span>
              </div>
            )}
          </div>
        ) : (
          <div>
            <label htmlFor={workspaceFieldId} className="label">
              Workspace
            </label>
            <select
              id={workspaceFieldId}
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
        )}

        <div>
          <label htmlFor={fileFieldId} className="label">
            Document
          </label>
          <div
            className={`relative flex min-h-32 flex-col items-center justify-center rounded-card border border-dashed px-5 py-6 text-center transition-colors ${
              dragging
                ? 'border-accent-ink bg-accent-soft'
                : 'border-line-strong bg-sunken/40 hover:border-ink-faint'
            }`}
            onDragEnter={() => setDragging(true)}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={() => setDragging(false)}
            onDrop={dropFile}
          >
            <UploadCloud aria-hidden="true" className="text-ink-faint" size={22} strokeWidth={1.7} />
            <p className="mt-2 text-sm font-medium text-ink">
              Drop a file here, or <span className="underline decoration-line-strong underline-offset-2">browse</span>
            </p>
            <div className="mt-2 flex gap-1.5">
              <span className="stamp">PDF</span>
              <span className="stamp">DOCX</span>
              <span className="stamp">CSV</span>
            </div>
            <input
              id={fileFieldId}
              ref={fileInput}
              className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
              type="file"
              accept=".pdf,.docx,.csv,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/csv"
              aria-describedby={helpId}
              disabled={uploading}
              onChange={chooseFile}
            />
          </div>
          <p id={helpId} className="mt-2 text-xs leading-5 text-ink-muted">
            PDF, DOCX or CSV. The server validates the file and enforces its size limit. A CSV is
            stored as rows and queried with SQL rather than indexed for meaning, so the agent can
            count, total and group its lines but not search their wording.
          </p>
          {file === null ? null : (
            <div className="mt-3 flex items-center gap-3 rounded-control border border-line bg-surface px-3.5 py-2.5 text-sm">
              <FileText aria-hidden="true" className="shrink-0 text-ink-muted" size={18} />
              <span className="min-w-0 flex-1 truncate text-ink">{file.name}</span>
              <span className="shrink-0 font-mono text-meta text-ink-muted">
                {formatBytes(file.size)}
              </span>
            </div>
          )}
        </div>

        <section aria-label="What happens next">
          <p className="label">What happens next</p>
          <ol className="space-y-3">
            {[
              ['01', 'Scan', 'ClamAV checks the file before anything is stored.'],
              ['02', 'Extract & chunk', 'The extractor reads pages, tables and pictures.'],
              ['03', 'Embed & index', 'The text becomes searchable in the workspace.'],
            ].map(([step, title, description]) => (
              <li key={step} className="flex items-start gap-3">
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-control bg-sunken font-mono text-[0.625rem] leading-none text-ink-muted">
                  {step}
                </span>
                <span className="min-w-0">
                  <span className="block text-[0.8125rem] font-medium text-ink">{title}</span>
                  <span className="mt-0.5 block text-xs leading-5 text-ink-muted">
                    {description}
                  </span>
                </span>
              </li>
            ))}
          </ol>
        </section>

        {error === null ? null : (
          <p className="error-note" role="alert">
            <CircleAlert aria-hidden="true" className="mt-0.5 shrink-0" size={16} />
            {error}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-3 border-t border-line pt-5">
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
          <p className="text-xs leading-5 text-ink-muted">
            Context indexing continues after upload.
          </p>
        </div>
      </form>

      {confirmation === null ? null : (
        <section
          className="mt-5 rounded-card border border-success/25 bg-success-soft px-4 py-4"
          role="status"
          aria-live="polite"
          aria-labelledby={`${formId}-result-title`}
        >
          <div className="flex items-start gap-3">
            <CheckCircle2 aria-hidden="true" className="mt-0.5 shrink-0 text-success" size={19} />
            <div className="min-w-0">
              <h2 id={`${formId}-result-title`} className="text-sm font-semibold text-success">
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
