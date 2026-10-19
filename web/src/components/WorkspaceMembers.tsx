import { useState, type FormEvent } from 'react'
import { Trash2, UserPlus } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { MemberCandidate, Workspace, WorkspaceMember, WorkspaceRole } from '../types'
import { ConfirmDialog } from './Dialog'
import { EmptyState } from './ui/Feedback'
import { SearchField } from './ui/SearchField'
import { useApiResource } from '../hooks/useApiResource'

interface WorkspaceMembersProps {
  workspace: Workspace
  token: string
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
 */
export function WorkspaceMembers({
  workspace,
  token,
  isAdmin,
  onChanged,
  onUnauthorized,
  onNotice,
}: WorkspaceMembersProps) {
  const [candidateSearch, setCandidateSearch] = useState('')
  const [candidates, setCandidates] = useState<MemberCandidate[]>([])
  const [selectedCandidateId, setSelectedCandidateId] = useState('')
  const [newRole, setNewRole] = useState<WorkspaceRole>('editor')
  const [searching, setSearching] = useState(false)
  const [adding, setAdding] = useState(false)
  const [updatingUserId, setUpdatingUserId] = useState<string | null>(null)
  const [removeTarget, setRemoveTarget] = useState<WorkspaceMember | null>(null)
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const members = useApiResource(
    (signal) => api.listMembers(token, workspace.id, isAdmin, signal),
    [token, workspace.id, isAdmin],
    { onUnauthorized },
  )

  async function searchCandidates(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const normalized = candidateSearch.trim()
    if (normalized.length < 2) {
      setError('Enter at least two characters to search for a user.')
      return
    }
    setSearching(true)
    setError(null)
    try {
      if (isAdmin) {
        const result = await api.listAdminUsers(token, { search: normalized })
        const existingIds = new Set(
          (members.data?.items ?? []).map((member) => member.user_id),
        )
        setCandidates(
          result.items
            .filter((user) => user.is_active && !existingIds.has(user.id))
            .map((user) => ({ id: user.id, username: user.username })),
        )
      } else {
        const result = await api.searchMemberCandidates(token, workspace.id, normalized)
        setCandidates(result.items)
      }
      setSelectedCandidateId('')
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else setError(getErrorMessage(caught))
    } finally {
      setSearching(false)
    }
  }

  async function addMember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (selectedCandidateId === '') return
    setAdding(true)
    setError(null)
    try {
      const member = await api.setMemberRole(
        token,
        workspace.id,
        selectedCandidateId,
        newRole,
        isAdmin,
      )
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
      setCandidates((current) =>
        current.filter((candidate) => candidate.id !== selectedCandidateId),
      )
      setSelectedCandidateId('')
      setCandidateSearch('')
      onNotice(`${member.username} added as ${member.role}.`)
      await onChanged()
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else setError(getErrorMessage(caught))
    } finally {
      setAdding(false)
    }
  }

  async function changeRole(member: WorkspaceMember, role: WorkspaceRole) {
    setUpdatingUserId(member.user_id)
    setError(null)
    try {
      const updated = await api.setMemberRole(
        token,
        workspace.id,
        member.user_id,
        role,
        isAdmin,
      )
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
      await onChanged()
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else setError(getErrorMessage(caught))
    } finally {
      setUpdatingUserId(null)
    }
  }

  async function removeMember() {
    if (removeTarget === null) return
    setRemoving(true)
    try {
      await api.removeMember(token, workspace.id, removeTarget.user_id, isAdmin)
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

  const items = members.data?.items ?? []

  return (
    <>
      <section className="border-b border-line px-5 py-5 sm:px-6">
        <p className="eyebrow">Add a member</p>
        <form className="mt-3 flex gap-2" role="search" onSubmit={searchCandidates}>
          <SearchField
            id="candidate-search"
            label="Search user candidates"
            value={candidateSearch}
            onChange={setCandidateSearch}
            placeholder="Search username"
            className="min-w-0 flex-1"
          />
          <button type="submit" className="btn btn-secondary" disabled={searching}>
            {searching ? 'Searching…' : 'Search'}
          </button>
        </form>
        {candidates.length === 0 ? null : (
          <form
            className="mt-3 grid gap-2 sm:grid-cols-[1fr_9rem_auto]"
            onSubmit={addMember}
          >
            <select
              className="field"
              aria-label="Candidate user"
              required
              value={selectedCandidateId}
              onChange={(event) => setSelectedCandidateId(event.target.value)}
            >
              <option value="">Select a user</option>
              {candidates.map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.username}
                </option>
              ))}
            </select>
            <select
              className="field"
              aria-label="Workspace role"
              value={newRole}
              onChange={(event) => setNewRole(event.target.value as WorkspaceRole)}
            >
              {isAdmin ? <option value="owner">Owner</option> : null}
              <option value="editor">Editor</option>
              <option value="viewer">Viewer</option>
            </select>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={adding || selectedCandidateId === ''}
            >
              <UserPlus aria-hidden="true" size={16} />
              {adding ? 'Adding…' : 'Add'}
            </button>
          </form>
        )}
      </section>

      <section className="px-5 py-5 sm:px-6">
        <div className="mb-3 flex items-center justify-between gap-4">
          <div>
            <p className="eyebrow">Access policy</p>
            <h3 className="mt-1 text-base font-semibold text-ink">Workspace members</h3>
          </div>
          <span className="font-mono text-meta text-ink-faint">{items.length} total</span>
        </div>
        {error === null ? null : (
          <p className="error-note mb-3" role="alert">
            {error}
          </p>
        )}
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
                <li
                  key={member.user_id}
                  className="grid gap-3 px-4 py-3.5 sm:grid-cols-[1fr_9rem_auto] sm:items-center"
                >
                  <div className="min-w-0">
                    <p className="truncate font-medium text-ink">{member.username}</p>
                    <p className="mt-0.5 font-mono text-meta text-ink-faint">
                      {member.user_id.slice(0, 8)}
                    </p>
                  </div>
                  <select
                    className="field text-sm"
                    aria-label={`Role for ${member.username}`}
                    value={member.role}
                    disabled={ownerLocked || updatingUserId === member.user_id}
                    onChange={(event) => changeRole(member, event.target.value as WorkspaceRole)}
                  >
                    {isAdmin || member.role === 'owner' ? (
                      <option value="owner">Owner</option>
                    ) : null}
                    <option value="editor">Editor</option>
                    <option value="viewer">Viewer</option>
                  </select>
                  <button
                    type="button"
                    className="btn-icon justify-self-end text-danger hover:text-danger"
                    aria-label={`Remove ${member.username}`}
                    disabled={ownerLocked}
                    onClick={() => setRemoveTarget(member)}
                  >
                    <Trash2 aria-hidden="true" size={16} />
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </section>

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
