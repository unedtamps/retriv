import { useState } from 'react'
import { FolderOpen, UserPlus, Users } from 'lucide-react'
import { api } from '../api'
import { useApiResource } from '../hooks/useApiResource'
import type { AdminWorkspace, WorkspaceMember } from '../types'
import { AddWorkspaceMemberDialog } from './AddWorkspaceMemberDialog'
import { Avatar } from './ui/Avatar'
import { WorkspaceMembersDialog } from './WorkspaceMembersDialog'

interface AdminWorkspacesProps {
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
}

/**
 * The platform-wide workspace register for admins: one slim block per
 * workspace, with the member register and the grant-membership action behind
 * two buttons. The register itself is read-only — membership changes run
 * through the dialogs, never through the block.
 */
export function AdminWorkspacesPage({ onUnauthorized, onNotice }: AdminWorkspacesProps) {
  const resource = useApiResource(
    (signal) => api.listAdminWorkspaces({ signal }),
    [],
    { onUnauthorized },
  )
  const workspaces = resource.data?.items ?? []

  return (
    <div className="mx-auto max-w-6xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <header className="border-b border-line pb-5">
        <p className="eyebrow">Platform administration</p>
        <h1 className="mt-1.5 font-condensed text-xl font-bold uppercase tracking-[0.05em] text-ink">Workspaces</h1>
        <p className="mt-1.5 max-w-2xl text-sm leading-6 text-ink-muted">
          Every workspace on the platform. Open a member register to see who holds access, or
          grant a new membership.
        </p>
      </header>

      <section className="mt-5 space-y-4" aria-label="All workspaces">
        {resource.error === null ? null : (
          <p className="error-note" role="alert">
            {resource.error}
          </p>
        )}
        {resource.loading && workspaces.length === 0 ? (
          <div className="space-y-3">
            {Array.from({ length: 3 }, (_, index) => (
              <div key={index} className="h-16 animate-pulse rounded-card bg-sunken" />
            ))}
          </div>
        ) : workspaces.length === 0 ? (
          <div className="flex flex-col items-center rounded-card border border-dashed border-line-strong bg-surface px-6 py-12 text-center">
            <FolderOpen aria-hidden="true" className="text-ink-faint" size={24} />
            <p className="mt-3 text-sm font-medium text-ink">No workspaces yet</p>
            <p className="mt-1 max-w-sm text-sm leading-6 text-ink-muted">
              Create the first workspace to start organizing documents and members.
            </p>
          </div>
        ) : (
          workspaces.map((workspace) => (
            <AdminWorkspaceBlock
              key={workspace.id}
              workspace={workspace}
              onUnauthorized={onUnauthorized}
              onNotice={onNotice}
              onChanged={resource.reload}
            />
          ))
        )}
      </section>
    </div>
  )
}

interface AdminWorkspaceBlockProps {
  workspace: AdminWorkspace
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onChanged: () => void
}

function AdminWorkspaceBlock({
  workspace,
  onUnauthorized,
  onNotice,
  onChanged,
}: AdminWorkspaceBlockProps) {
  const [membersOpen, setMembersOpen] = useState(false)
  const [addOpen, setAddOpen] = useState(false)

  function handleAdded(member: WorkspaceMember) {
    setAddOpen(false)
    onNotice(`${member.username} added to ${workspace.name} as ${member.role}.`)
    onChanged()
  }

  return (
    <article className="rounded-card border border-line bg-surface">
      <div className="flex items-center gap-3 px-4 py-4 sm:px-5">
        <Avatar name={workspace.name} seed={workspace.id} size="sm" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[0.9375rem] font-medium text-ink">{workspace.name}</p>
          <p className="mt-0.5 truncate text-xs text-ink-muted">
            {workspace.description ?? workspace.slug}
          </p>
        </div>
        <span className="shrink-0 font-mono text-[0.7rem] text-ink-muted">
          {workspace.member_count} {workspace.member_count === 1 ? 'member' : 'members'}
        </span>
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            className="btn-icon h-8 w-8 border-transparent bg-transparent hover:border-line"
            aria-label={`View members of ${workspace.name}`}
            onClick={() => setMembersOpen(true)}
          >
            <Users aria-hidden="true" size={16} />
          </button>
          <button
            type="button"
            className="btn-icon h-8 w-8 border-transparent bg-transparent hover:border-line"
            aria-label={`Add a member to ${workspace.name}`}
            onClick={() => setAddOpen(true)}
          >
            <UserPlus aria-hidden="true" size={16} />
          </button>
        </div>
      </div>

      {membersOpen ? (
        <WorkspaceMembersDialog
          open
          workspace={workspace}
          onClose={() => setMembersOpen(false)}
        />
      ) : null}
      {addOpen ? (
        <AddWorkspaceMemberDialog
          open
          workspace={workspace}
          onUnauthorized={onUnauthorized}
          onAdded={handleAdded}
          onClose={() => setAddOpen(false)}
        />
      ) : null}
    </article>
  )
}
