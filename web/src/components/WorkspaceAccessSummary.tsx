import { ArrowUpRight, CheckCircle2, CircleSlash2, Users } from 'lucide-react'
import { api } from '../api'
import type { Workspace } from '../types'
import { useApiResource } from '../hooks/useApiResource'
import { Avatar } from './ui/Avatar'

interface WorkspaceAccessSummaryProps {
  workspace: Workspace
  isAdmin: boolean
  onManageMembers: () => void
  onUnauthorized: () => void
}

export function WorkspaceAccessSummary({
  workspace,
  isAdmin,
  onManageMembers,
  onUnauthorized,
}: WorkspaceAccessSummaryProps) {
  const canManage = isAdmin || workspace.can_manage_members
  const members = useApiResource(
    async (signal) => {
      if (!canManage) return { items: [] }
      return api.listMembers(workspace.id, isAdmin, signal)
    },
    [workspace.id, isAdmin, canManage],
    { onUnauthorized },
  )
  const items = members.data?.items ?? []

  return (
    <section className="rounded-card border border-line bg-surface p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="eyebrow">Access</p>
          <h2 className="mt-1.5 text-sm font-semibold text-ink">Who can use this workspace</h2>
        </div>
        <span className="badge shrink-0">{workspace.membership_role ?? 'catalog'}</span>
      </div>

      {canManage ? (
        <>
          <div className="mt-4 flex min-h-8 items-center">
            {members.loading ? (
              <div className="flex gap-1.5" aria-label="Loading workspace members">
                {Array.from({ length: 3 }, (_, index) => (
                  <span key={index} className="h-8 w-8 animate-pulse rounded-md bg-sunken" />
                ))}
              </div>
            ) : items.length === 0 ? (
              <p className="text-sm leading-6 text-ink-muted">
                No members are listed yet. Add an owner or editor to share this workspace.
              </p>
            ) : (
              <div className="flex items-center">
                <div className="flex -space-x-1.5">
                  {items.slice(0, 4).map((member) => (
                    <Avatar
                      key={member.user_id}
                      name={member.username}
                      seed={member.user_id}
                      size="sm"
                      className="ring-2 ring-surface"
                    />
                  ))}
                </div>
                <div className="ml-3">
                  <p className="text-sm font-medium text-ink">
                    {items.length} member{items.length === 1 ? '' : 's'}
                  </p>
                  <p className="text-meta text-ink-faint">
                    {items.filter((member) => member.role === 'owner').length} owner
                    {items.filter((member) => member.role === 'owner').length === 1 ? '' : 's'}
                  </p>
                </div>
              </div>
            )}
          </div>
          {members.error === null ? null : (
            <p className="mt-3 text-xs leading-5 text-danger" role="alert">
              {members.error}
            </p>
          )}
          <button
            type="button"
            className="mt-4 inline-flex items-center gap-1.5 text-xs font-medium text-ink-muted transition-colors hover:text-ink"
            onClick={onManageMembers}
          >
            <Users aria-hidden="true" size={14} />
            Manage members
            <ArrowUpRight aria-hidden="true" size={13} />
          </button>
        </>
      ) : (
        <>
          <p className="mt-3 text-sm leading-6 text-ink-muted">
            {workspace.membership_role === null
              ? 'You can discover this workspace, but its documents and context remain inaccessible.'
              : 'Your role decides which document actions are available. Membership changes take effect immediately.'}
          </p>
          <ul className="mt-4 space-y-2">
            <Capability label="Upload documents" enabled={workspace.can_upload} />
            <Capability label="Search context" enabled={workspace.can_search_context} />
            <Capability label="Delete documents" enabled={workspace.can_delete} />
          </ul>
        </>
      )}
    </section>
  )
}

function Capability({ label, enabled }: { label: string; enabled: boolean }) {
  return (
    <li className="flex items-center gap-2 text-xs text-ink-muted">
      {enabled ? (
        <CheckCircle2 aria-hidden="true" className="text-success" size={13} />
      ) : (
        <CircleSlash2 aria-hidden="true" className="text-ink-faint" size={13} />
      )}
      {label}
    </li>
  )
}
