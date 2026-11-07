import type { Workspace } from '../types'
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

  if (!canManage) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="font-condensed text-xl font-bold uppercase tracking-[0.05em] text-ink">
            Members
          </h1>
          <span className="stamp">restricted</span>
        </div>
        <section className="mt-4 rounded-card border border-line bg-surface px-6 py-12 text-center">
          <p className="text-sm font-semibold text-ink">Membership management unavailable</p>
          <p className="mx-auto mt-1.5 max-w-sm text-sm leading-6 text-ink-muted">
            Only workspace owners and platform administrators can view or change this access
            policy.
          </p>
        </section>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <WorkspaceMembers
        workspace={workspace}
        isAdmin={isAdmin}
        onChanged={onChanged}
        onUnauthorized={onUnauthorized}
        onNotice={onNotice}
      />
    </div>
  )
}
