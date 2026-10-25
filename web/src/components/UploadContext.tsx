import { ArrowRight, LockKeyhole, ShieldCheck } from 'lucide-react'
import type { Workspace } from '../types'
import { UploadForm } from './UploadForm'
import { Panel, PanelHeader } from './ui/Panel'

interface UploadContextProps {
  token: string
  workspaces: Workspace[]
  onUnauthorized: () => void
}

export function UploadContext({ token, workspaces, onUnauthorized }: UploadContextProps) {
  const availableWorkspaces = workspaces.filter((workspace) => workspace.can_upload)

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
            <LockKeyhole className="mx-auto text-ink-faint" aria-hidden="true" size={28} />
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
            <div className="px-5 py-6">
              <UploadForm
                token={token}
                workspaces={workspaces}
                onUnauthorized={onUnauthorized}
              />
            </div>
          </Panel>

          <aside className="rounded-card border border-line bg-surface p-5" aria-label="Upload policy">
            <ShieldCheck aria-hidden="true" className="text-teal" size={22} />
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
    </div>
  )
}
