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

interface UploadContextProps {
  token: string
  workspaces: Workspace[]
  onUnauthorized: () => void
}

interface UploadConfirmation {
  document: DocumentUpload
  workspaceName: string
}

const supportedFile = /\.(pdf|docx)$/i

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
        ? 'Only PDF and DOCX files can be processed as context.'
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
      setError('Choose a PDF or DOCX file to upload.')
      return
    }
    if (!supportedFile.test(file.name)) {
      setError('Only PDF and DOCX files can be processed as context.')
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
    <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-10 lg:py-10">
      <header className="border-b border-stone-300 pb-8">
        <p className="section-eyebrow">Document intake</p>
        <h1 className="mt-3 text-3xl font-semibold tracking-[-0.04em] text-stone-950 sm:text-4xl">
          Upload context
        </h1>
        <p className="mt-3 max-w-2xl text-sm leading-6 text-stone-600 sm:text-base">
          Add a document to a workspace. Once accepted, its content is extracted and indexed in the background.
        </p>
      </header>

      {availableWorkspaces.length === 0 ? (
        <section className="mt-8 border border-stone-300 bg-white px-6 py-12 text-center sm:px-10" aria-labelledby="no-upload-access">
          <LockKeyhole className="mx-auto text-stone-500" aria-hidden="true" size={30} />
          <h2 id="no-upload-access" className="mt-4 text-xl font-semibold text-stone-950">No workspaces available for upload</h2>
          <p className="mx-auto mt-2 max-w-lg text-sm leading-6 text-stone-600">
            Ask a workspace owner for owner or editor access. A platform admin role alone does not grant document access.
          </p>
          <a href="/dashboard" className="button-secondary mt-6">View workspaces <ArrowRight aria-hidden="true" size={16} /></a>
        </section>
      ) : (
        <div className="mt-8 grid gap-6 lg:grid-cols-[minmax(0,1fr)_17rem] lg:items-start">
          <section className="border border-stone-300 bg-white" aria-labelledby="upload-form-title">
            <div className="border-b border-stone-200 px-5 py-5 sm:px-7">
              <p className="section-eyebrow">New document</p>
              <h2 id="upload-form-title" className="mt-1 text-xl font-semibold text-stone-950">Select its destination and file</h2>
            </div>
            <form className="space-y-7 px-5 py-6 sm:px-7 sm:py-8" onSubmit={submitUpload}>
              <div>
                <label htmlFor="upload-workspace" className="field-label">Workspace</label>
                <select
                  id="upload-workspace"
                  className="field-input"
                  value={workspaceId}
                  disabled={uploading}
                  onChange={(event) => {
                    setWorkspaceId(event.target.value)
                    setError(null)
                  }}
                >
                  <option value="">Choose a workspace</option>
                  {availableWorkspaces.map((workspace) => (
                    <option key={workspace.id} value={workspace.id}>{workspace.name} · {workspace.slug}</option>
                  ))}
                </select>
                {selectedWorkspace === undefined ? (
                  <p className="mt-2 text-xs leading-5 text-stone-600">Only workspaces where you are an owner or editor are shown.</p>
                ) : (
                  <p className="mt-2 text-xs leading-5 text-stone-600">
                    {selectedWorkspace.description ?? selectedWorkspace.slug} · Your role: {selectedWorkspace.membership_role}
                  </p>
                )}
              </div>

              <div>
                <label htmlFor="upload-file" className="field-label">Document</label>
                <input
                  id="upload-file"
                  ref={fileInput}
                  className="field-input cursor-pointer text-sm file:mr-4 file:cursor-pointer file:border-0 file:border-r file:border-stone-200 file:bg-stone-50 file:px-3 file:py-1 file:text-sm file:font-medium file:text-stone-800"
                  type="file"
                  accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                  aria-describedby="upload-file-help"
                  disabled={uploading}
                  onChange={chooseFile}
                />
                <p id="upload-file-help" className="mt-2 text-xs leading-5 text-stone-600">
                  PDF or DOCX. The server validates the file and enforces its size limit. Spreadsheets are not yet indexed as context.
                </p>
                {file === null ? null : (
                  <div className="mt-4 flex items-center gap-3 border border-stone-200 bg-stone-50 px-4 py-3 text-sm">
                    <FileText aria-hidden="true" className="shrink-0 text-emerald-800" size={19} />
                    <span className="min-w-0 flex-1 truncate text-stone-950">{file.name}</span>
                    <span className="shrink-0 font-mono text-xs text-stone-600">{formatBytes(file.size)}</span>
                  </div>
                )}
              </div>

              {error === null ? null : (
                <p className="form-error flex items-start gap-2" role="alert"><CircleAlert aria-hidden="true" className="mt-0.5 shrink-0" size={17} />{error}</p>
              )}

              <div className="flex flex-wrap items-center gap-4 border-t border-stone-200 pt-6">
                <button type="submit" className="button-primary min-w-40" disabled={uploading || file === null || file.size === 0 || !supportedFile.test(file.name) || selectedWorkspace === undefined}>
                  {uploading ? <LoaderCircle aria-hidden="true" className="animate-spin" size={17} /> : <FileUp aria-hidden="true" size={17} />}
                  {uploading ? 'Uploading…' : 'Upload document'}
                </button>
                <p className="text-xs text-stone-600">Context indexing continues after upload.</p>
              </div>
            </form>
          </section>

          <aside className="border border-stone-300 bg-[#17231f] p-6 text-white" aria-label="Upload policy">
            <ShieldCheck aria-hidden="true" className="text-[#d9f15f]" size={25} />
            <h2 className="mt-5 text-base font-semibold">Workspace-first access</h2>
            <p className="mt-2 text-sm leading-6 text-stone-300">
              Every document belongs to the workspace you choose. Membership is checked again by the API when you submit.
            </p>
            <p className="mt-5 border-t border-white/15 pt-5 font-mono text-[0.68rem] uppercase tracking-[0.12em] text-[#d9f15f]">
              {availableWorkspaces.length} available workspace{availableWorkspaces.length === 1 ? '' : 's'}
            </p>
          </aside>
        </div>
      )}

      {confirmation === null ? null : (
        <section className="mt-6 border border-emerald-700 bg-emerald-50 px-5 py-5 sm:px-7" role="status" aria-live="polite" aria-labelledby="upload-result-title">
          <div className="flex items-start gap-3">
            <CheckCircle2 aria-hidden="true" className="mt-0.5 shrink-0 text-emerald-800" size={21} />
            <div className="min-w-0">
              <h2 id="upload-result-title" className="font-semibold text-emerald-950">
                {confirmation.document.duplicate ? 'Document already exists' : 'Document accepted'}
              </h2>
              <p className="mt-1 text-sm leading-6 text-emerald-950">
                {confirmation.document.duplicate
                  ? 'An identical document already exists in this workspace; no new upload was created.'
                  : 'The file is stored. Context extraction and indexing happen asynchronously.'}
              </p>
              <p className="mt-3 break-all font-mono text-xs text-emerald-900">
                {confirmation.document.original_filename} · {confirmation.workspaceName} · ID {confirmation.document.id}
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
