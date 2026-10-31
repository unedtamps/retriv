import { useState } from 'react'
import { Pencil, Trash2, UserPlus } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { Workspace, WorkspaceMember } from '../types'
import { AddWorkspaceMemberDialog } from './AddMemberToWorkspaceDialog'
import { ChangeMemberAccessDialog } from './ChangeMemberAccessDialog'
import { ConfirmDialog } from './Dialog'
import { Avatar } from './ui/Avatar'
import { EmptyState } from './ui/Feedback'
import { useApiResource } from '../hooks/useApiResource'

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

  return (
    <>
      <section className="px-5 py-5 sm:px-6">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="eyebrow">Access policy</p>
            <div className="mt-1 flex items-baseline gap-2">
              <h3 className="text-base font-semibold text-ink">Workspace members</h3>
              <span className="font-mono text-meta text-ink-faint">{items.length}</span>
            </div>
          </div>
          <button type="button" className="btn btn-secondary" onClick={() => setAddOpen(true)}>
            <UserPlus aria-hidden="true" size={16} />
            Add member
          </button>
        </div>

        {members.error === null ? null : (
          <p className="error-note mb-3" role="alert">
            {members.error}
          </p>
        )}

        {members.loading && items.length === 0 ? (
          <div className="space-y-2" aria-label="Loading workspace members">
            {Array.from({ length: 3 }, (_, index) => (
              <div key={index} className="h-16 animate-pulse rounded-card bg-sunken" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <EmptyState title="No workspace members" />
        ) : (
          <ul className="divide-y divide-line rounded-card border border-line">
            {items.map((member) => {
              const ownerLocked = !isAdmin && member.role === 'owner'
              return (
                <li key={member.user_id} className="flex items-center gap-4 px-4 py-3.5 sm:px-5">
                  <Avatar name={member.username} seed={member.user_id} size="xs" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-ink">{member.username}</p>
                    <p className="mt-0.5 font-mono text-meta text-ink-faint">
                      {member.user_id.slice(0, 8)}
                    </p>
                  </div>
                  <span className="stamp shrink-0">{member.role}</span>
                  <button
                    type="button"
                    className="btn-icon h-8 w-8 border-transparent bg-transparent hover:border-line disabled:opacity-40"
                    aria-label={`Change access for ${member.username}`}
                    disabled={ownerLocked}
                    onClick={() => setEditTarget(member)}
                  >
                    <Pencil aria-hidden="true" size={15} />
                  </button>
                  <button
                    type="button"
                    className="btn-icon h-8 w-8 border-transparent bg-transparent text-danger hover:border-line disabled:opacity-40"
                    aria-label={`Remove ${member.username}`}
                    disabled={ownerLocked}
                    onClick={() => setRemoveTarget(member)}
                  >
                    <Trash2 aria-hidden="true" size={15} />
                  </button>
                </li>
              )
            })}
          </ul>
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
