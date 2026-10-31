import { useEffect, useMemo, useState } from 'react'
import { Pencil, Trash2, UserPlus } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { Workspace, WorkspaceMember, WorkspaceRole } from '../types'
import { ChangeMemberAccessDialog } from './ChangeMemberAccessDialog'
import { ConfirmDialog } from './Dialog'
import { Avatar } from './ui/Avatar'
import { EmptyState } from './ui/Feedback'
import { RoleTiles } from './ui/RoleTiles'
import { UserSearchRoster, type UserRosterEntry } from './ui/UserSearchRoster'
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
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [loadedQuery, setLoadedQuery] = useState<string | null>(null)
  const [entries, setEntries] = useState<UserRosterEntry[]>([])
  const [total, setTotal] = useState(0)
  const [selectedCandidateId, setSelectedCandidateId] = useState('')
  const [newRole, setNewRole] = useState<WorkspaceRole>('viewer')
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [editTarget, setEditTarget] = useState<WorkspaceMember | null>(null)
  const [removeTarget, setRemoveTarget] = useState<WorkspaceMember | null>(null)
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const members = useApiResource(
    (signal) => api.listMembers(token, workspace.id, isAdmin, signal),
    [token, workspace.id, isAdmin],
    { onUnauthorized },
  )

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(candidateSearch.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [candidateSearch])

  useEffect(() => {
    const controller = new AbortController()
    const load = async () => {
      if (isAdmin) {
        const result = await api.listAdminUsers(token, {
          search: debouncedSearch,
          signal: controller.signal,
        })
        setEntries(
          result.items
            .filter((user) => user.is_active && user.role !== 'admin')
            .map((user) => ({ id: user.id, username: user.username, role: user.role })),
        )
        setTotal(result.total)
      } else {
        if (debouncedSearch.length < 2) {
          setEntries([])
          setTotal(0)
        } else {
          const result = await api.searchMemberCandidates(token, workspace.id, debouncedSearch)
          setEntries(result.items)
          setTotal(result.items.length)
        }
      }
      setLoadedQuery(debouncedSearch)
    }
    load().catch((caught: unknown) => {
      if (caught instanceof Error && caught.name === 'AbortError') return
      setLoadedQuery(debouncedSearch)
      if (isUnauthorized(caught)) onUnauthorized()
      else setError(getErrorMessage(caught))
    })
    return () => controller.abort()
  }, [debouncedSearch, isAdmin, token, workspace.id, onUnauthorized])

  const existingIds = useMemo(
    () => new Set((members.data?.items ?? []).map((member) => member.user_id)),
    [members.data],
  )
  const candidates = entries.filter((entry) => !existingIds.has(entry.id))
  const searching = loadedQuery !== debouncedSearch
  const items = members.data?.items ?? []

  async function addMember() {
    if (selectedCandidateId === '') return
    setAdding(true)
    setAddError(null)
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
      setSelectedCandidateId('')
      setCandidateSearch('')
      onNotice(`${member.username} added as ${member.role}.`)
      await onChanged()
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else setAddError(getErrorMessage(caught))
    } finally {
      setAdding(false)
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

  const emptyHint =
    !isAdmin && debouncedSearch.length < 2
      ? 'Type at least two characters to search for a user.'
      : 'No matching user.'

  return (
    <>
      <section className="border-b border-line px-6 py-6">
        <p className="eyebrow">Add a member</p>
        <div className="mt-4">
          <UserSearchRoster
            id="candidate-search"
            label="Search users"
            search={candidateSearch}
            onSearchChange={setCandidateSearch}
            candidates={candidates}
            searching={searching}
            selectedId={selectedCandidateId}
            onSelect={setSelectedCandidateId}
            emptyMessage={emptyHint}
            total={total}
          />
        </div>
        <div className="mt-5 grid gap-4 sm:grid-cols-[minmax(0,20rem)_auto] sm:items-end">
          <RoleTiles
            label="Workspace role"
            labelId="candidate-role-label"
            value={newRole}
            onChange={setNewRole}
            roles={isAdmin ? ['owner', 'editor', 'viewer'] : ['editor', 'viewer']}
          />
          <button
            type="button"
            className="btn btn-primary sm:mb-0.5"
            disabled={adding || selectedCandidateId === ''}
            onClick={addMember}
          >
            <UserPlus aria-hidden="true" size={16} />
            {adding ? 'Adding…' : 'Add member'}
          </button>
        </div>
        {addError === null ? null : (
          <p className="error-note mt-4" role="alert">
            {addError}
          </p>
        )}
      </section>

      <section className="px-6 py-6">
        <div className="mb-4 flex items-center justify-between gap-4">
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
                <li key={member.user_id} className="flex items-center gap-4 px-5 py-4">
                  <Avatar name={member.username} seed={member.user_id} size="xs" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium text-ink">{member.username}</p>
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

      {editTarget === null ? null : (
        <ChangeMemberAccessDialog
          open
          workspace={workspace}
          token={token}
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
