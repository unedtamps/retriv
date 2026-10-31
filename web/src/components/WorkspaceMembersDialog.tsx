import type { AdminWorkspace } from '../types'
import { UserRound } from 'lucide-react'
import { Dialog } from './Dialog'
import { Avatar } from './ui/Avatar'
import { EmptyState } from './ui/Feedback'

interface WorkspaceMembersDialogProps {
  open: boolean
  workspace: AdminWorkspace
  onClose: () => void
}

/**
 * The member register of one workspace, for the platform administrator.
 *
 * Read-only on purpose: the register answers "who is in here and at what
 * role", while granting and revoking live in the add-member dialog and the
 * API. The list arrives embedded in the workspace itself, so opening the
 * register costs no request.
 */
export function WorkspaceMembersDialog({ open, workspace, onClose }: WorkspaceMembersDialogProps) {
  return (
    <Dialog
      open={open}
      eyebrow={workspace.name}
      title="Workspace members"
      description="Roles resolve document permissions on every request, so a change here applies immediately."
      onClose={onClose}
    >
      {workspace.members.length === 0 ? (
        <EmptyState
          icon={<UserRound aria-hidden="true" size={20} />}
          title="No members yet"
          description="Grant the first membership with the add-member action beside this workspace."
        />
      ) : (
        <ul className="divide-y divide-line px-2 py-1">
          {workspace.members.map((member) => (
            <li key={member.user_id} className="flex items-center gap-3 px-3 py-3">
              <Avatar name={member.username} seed={member.user_id} size="xs" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-ink">{member.username}</p>
                <p className="mt-0.5 font-mono text-meta text-ink-faint">
                  {member.user_id.slice(0, 8)}
                </p>
              </div>
              <span className="stamp shrink-0">{member.role}</span>
            </li>
          ))}
        </ul>
      )}
      <footer className="border-t border-line px-5 py-2.5 font-mono text-meta text-ink-faint sm:px-6">
        {workspace.members.length} {workspace.members.length === 1 ? 'member' : 'members'}
      </footer>
    </Dialog>
  )
}
