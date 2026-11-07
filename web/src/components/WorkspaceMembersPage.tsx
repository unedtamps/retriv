import { CircleSlash2 } from 'lucide-react'
import type { Workspace } from '../types'
import { Avatar } from './ui/Avatar'
import { WorkspaceAccess } from './WorkspaceAccess'
import { WorkspaceMembers } from './WorkspaceMembers'

interface WorkspaceMembersPageProps {
  workspace: Workspace
  isAdmin: boolean
  onChanged: () => Promise<void>
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
}

export function WorkspaceMembersPage({
  workspace,
  isAdmin,
  onChanged,
  onUnauthorized,
  onNotice,
}: WorkspaceMembersPageProps) {
  const canManage = isAdmin || workspace.can_manage_members

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <header className="flex items-start gap-3.5">
        <Avatar name={workspace.name} seed={workspace.id} size="lg" className="mt-0.5" />
        <div className="min-w-0">
          <p className="eyebrow">Members and access</p>
          <h1 className="mt-1.5 truncate font-condensed text-xl font-bold uppercase tracking-[0.05em] text-ink">
            {workspace.name}
          </h1>
          <p className="mt-1.5 max-w-2xl text-sm leading-6 text-ink-muted">
            Membership is resolved by the API on every request. Changes take effect immediately
            for documents, search context and MCP clients.
          </p>
        </div>
      </header>

      <div className="mt-6 overflow-hidden rounded-card border border-line bg-surface">
        <WorkspaceAccess
          workspace={workspace}
          isAdmin={isAdmin}
          canManage={canManage}
          canDelete={workspace.can_delete}
        />
        {canManage ? (
          <WorkspaceMembers
            workspace={workspace}
            isAdmin={isAdmin}
            onChanged={onChanged}
            onUnauthorized={onUnauthorized}
            onNotice={onNotice}
          />
        ) : (
          <div className="px-6 py-12 text-center">
            <CircleSlash2 aria-hidden="true" className="mx-auto text-ink-faint" size={24} />
            <h2 className="mt-3 text-sm font-semibold text-ink">Membership management unavailable</h2>
            <p className="mx-auto mt-1.5 max-w-sm text-sm leading-6 text-ink-muted">
              Only workspace owners and platform administrators can view or change this access
              policy.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
