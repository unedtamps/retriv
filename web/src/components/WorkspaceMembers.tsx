import { useState } from 'react'
import { Pencil, Trash2, UserPlus } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { Workspace, WorkspaceMember, WorkspaceRole } from '../types'
import { AddWorkspaceMemberDialog } from './AddMemberToWorkspaceDialog'
import { ChangeMemberAccessDialog } from './ChangeMemberAccessDialog'
import { ConfirmDialog } from './Dialog'
import { Avatar } from './ui/Avatar'
import { EmptyState } from './ui/Feedback'
import { useApiResource } from '../hooks/useApiResource'
import { formatShortDate } from '../lib/visuals'

const ROLE_ORDER: Record<WorkspaceRole, number> = { owner: 0, editor: 1, viewer: 2 }

const ROLE_CHIP: Record<WorkspaceRole, string> = {
  owner: 'border-ink bg-ink text-surface',
  editor: 'border-success/50 bg-surface text-success',
  viewer: 'border-line bg-surface text-ink-muted',
}

interface WorkspaceMembersProps {
  workspace: Workspace
  isAdmin: boolean
  onChanged: () => Promise<void>
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
}

/**
 * The access policy: who is in the workspace and what they may do.
 *
 * Membership role only. The API resolves document permissions through the
 * caller's membership with no platform-admin bypass, so offering an admin a
 * button here would render one that answers 404.
 *
 * Adding a member lives behind a dialog; the page itself is the list people
 * came here to read.
 */
export function WorkspaceMembers({
  workspace,
  isAdmin,
  onChanged,
  onUnauthorized,
  onNotice,
}: WorkspaceMembersProps) {
  const [addOpen, setAddOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<WorkspaceMember | null>(null)
  const [removeTarget, setRemoveTarget] = useState<WorkspaceMember | null>(null)
  const [removing, setRemoving] = useState(false)

  const members = useApiResource(
    (signal) => api.listMembers(workspace.id, isAdmin, signal),
    [workspace.id, isAdmin],
    { onUnauthorized },
  )

  const items = members.data?.items ?? []

  async function removeMember() {
    if (removeTarget === null) return
    setRemoving(true)
    try {
      await api.removeMember(workspace.id, removeTarget.user_id, isAdmin)
      members.setData((current) =>
        current === null
          ? current
          : {
              ...current,
              items: current.items.filter((member) => member.user_id !== removeTarget.user_id),
            },
      )
      onNotice(`${removeTarget.username} removed from ${workspace.name}.`)
      setRemoveTarget(null)
      await onChanged()
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setRemoving(false)
    }
  }

  const register = [...items].sort(
    (a, b) =>
      ROLE_ORDER[a.role] - ROLE_ORDER[b.role] || a.username.localeCompare(b.username),
  )

  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-condensed text-xl font-bold uppercase tracking-[0.05em] text-ink">
          Members
        </h1>
        <span className="stamp">{items.length} enrolled</span>
        <span className="ml-auto" />
        <button type="button" className="btn btn-primary" onClick={() => setAddOpen(true)}>
          <UserPlus aria-hidden="true" size={14} />
          Add member
        </button>
      </div>

      {members.error === null ? null : (
        <p className="error-note mt-4" role="alert">
          {members.error}
        </p>
      )}

      <section className="mt-4 overflow-hidden rounded-card border border-line bg-surface">
        <div className="flex items-center gap-2.5 border-b border-line px-4 py-2.5">
          <p className="eyebrow">Register of members</p>
          <span className="ml-auto font-mono text-[0.65rem] uppercase tracking-[0.08em] text-ink-faint">
            sorted by role
          </span>
        </div>

        {members.loading && items.length === 0 ? (
          <div className="space-y-2 p-4" aria-label="Loading workspace members">
            {Array.from({ length: 3 }, (_, index) => (
              <div key={index} className="h-14 animate-pulse rounded-card bg-sunken" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <EmptyState title="No workspace members" />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">User</th>
                <th scope="col">Role</th>
                <th scope="col">Joined</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {register.map((member) => {
                const ownerLocked = !isAdmin && member.role === 'owner'
                return (
                  <tr key={member.user_id}>
                    <td>
                      <div className="flex items-center gap-3">
                        <Avatar name={member.username} seed={member.user_id} size="sm" />
                        <div className="min-w-0">
                          <p className="truncate text-sm font-semibold text-ink">
                            {member.username}
                          </p>
                          <p className="font-mono text-[0.65rem] text-ink-faint">
                            {member.user_id.slice(0, 8)}
                          </p>
                        </div>
                      </div>
                    </td>
                    <td>
                      <span className={`stamp ${ROLE_CHIP[member.role]}`}>{member.role}</span>
                    </td>
                    <td className="font-mono text-meta text-ink-muted">
                      {formatShortDate(member.created_at)}
                    </td>
                    <td>
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          type="button"
                          className="flex h-7 w-7 items-center justify-center rounded-stamp border border-transparent text-ink-faint transition-colors hover:border-line hover:text-ink disabled:opacity-40"
                          aria-label={`Change access for ${member.username}`}
                          disabled={ownerLocked}
                          onClick={() => setEditTarget(member)}
                        >
                          <Pencil aria-hidden="true" size={14} />
                        </button>
                        <button
                          type="button"
                          className="flex h-7 w-7 items-center justify-center rounded-stamp border border-transparent text-ink-faint transition-colors hover:border-danger/50 hover:text-danger disabled:opacity-40"
                          aria-label={`Remove ${member.username}`}
                          disabled={ownerLocked}
                          onClick={() => setRemoveTarget(member)}
                        >
                          <Trash2 aria-hidden="true" size={14} />
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </section>

      {addOpen ? (
        <AddWorkspaceMemberDialog
          open
          workspace={workspace}
          isAdmin={isAdmin}
          existingMemberIds={items.map((member) => member.user_id)}
          onUnauthorized={onUnauthorized}
          onAdded={(member) => {
            members.setData((current) =>
              current === null
                ? current
                : {
                    ...current,
                    items: [...current.items, member].toSorted((a, b) =>
                      a.username.localeCompare(b.username),
                    ),
                  },
            )
            setAddOpen(false)
            onNotice(`${member.username} added as ${member.role}.`)
            void onChanged()
          }}
          onClose={() => setAddOpen(false)}
        />
      ) : null}

      {editTarget === null ? null : (
        <ChangeMemberAccessDialog
          open
          workspace={workspace}
          member={editTarget}
          isAdmin={isAdmin}
          onUnauthorized={onUnauthorized}
          onSaved={(updated) => {
            members.setData((current) =>
              current === null
                ? current
                : {
                    ...current,
                    items: current.items.map((item) =>
                      item.user_id === updated.user_id ? updated : item,
                    ),
                  },
            )
            onNotice(`${updated.username} is now ${updated.role}.`)
            setEditTarget(null)
            void onChanged()
          }}
          onClose={() => setEditTarget(null)}
        />
      )}

      <ConfirmDialog
        open={removeTarget !== null}
        title="Remove workspace access?"
        description={`${removeTarget?.username ?? 'This user'} will immediately lose access granted by this workspace membership.`}
        confirmLabel="Remove member"
        busy={removing}
        destructive
        onClose={() => setRemoveTarget(null)}
        onConfirm={removeMember}
      />
    </>
  )
}
