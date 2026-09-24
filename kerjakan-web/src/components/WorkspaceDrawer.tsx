import { useEffect, useState, type FormEvent } from 'react'
import {
  CheckCircle2,
  CircleSlash2,
  FileUp,
  Search,
  Sparkles,
  Trash2,
  UserPlus,
  Users,
} from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type {
  MemberCandidate,
  Workspace,
  WorkspaceMember,
  WorkspaceRole,
} from '../types'
import { ConfirmDialog, Dialog } from './Dialog'

interface WorkspaceDrawerProps {
  workspace: Workspace
  token: string
  isAdmin: boolean
  onChanged: () => Promise<void>
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onClose: () => void
}

export function WorkspaceDrawer({
  workspace,
  token,
  isAdmin,
  onChanged,
  onUnauthorized,
  onNotice,
  onClose,
}: WorkspaceDrawerProps) {
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [loadingMembers, setLoadingMembers] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [candidateSearch, setCandidateSearch] = useState('')
  const [candidates, setCandidates] = useState<MemberCandidate[]>([])
  const [selectedCandidateId, setSelectedCandidateId] = useState('')
  const [newRole, setNewRole] = useState<WorkspaceRole>('editor')
  const [searching, setSearching] = useState(false)
  const [adding, setAdding] = useState(false)
  const [updatingUserId, setUpdatingUserId] = useState<string | null>(null)
  const [removeTarget, setRemoveTarget] = useState<WorkspaceMember | null>(null)
  const [removing, setRemoving] = useState(false)

  const canManage = isAdmin || workspace.can_manage_members

  useEffect(() => {
    if (!canManage) return
    const controller = new AbortController()
    api
      .listMembers(token, workspace.id, isAdmin, controller.signal)
      .then((result) => {
        setMembers(result.items)
        setLoadingMembers(false)
      })
      .catch((caught: unknown) => {
        if (caught instanceof Error && caught.name === 'AbortError') return
        if (isUnauthorized(caught)) onUnauthorized()
        else setError(getErrorMessage(caught))
        setLoadingMembers(false)
      })
    return () => controller.abort()
  }, [workspace, canManage, token, isAdmin, onUnauthorized])

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
        const existingIds = new Set(members.map((member) => member.user_id))
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
      setMembers((current) => [...current, member].toSorted((a, b) => a.username.localeCompare(b.username)))
      setCandidates((current) => current.filter((candidate) => candidate.id !== selectedCandidateId))
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
      setMembers((current) =>
        current.map((item) => (item.user_id === updated.user_id ? updated : item)),
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
      setMembers((current) => current.filter((member) => member.user_id !== removeTarget.user_id))
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
      <Dialog
        open
        variant="drawer"
        title={workspace.name}
        eyebrow={workspace.slug}
        description={workspace.description ?? 'No workspace description has been provided.'}
        onClose={onClose}
      >
        <div className="drawer-scroll">
          <section className="grid grid-cols-2 border-b border-stone-200 sm:grid-cols-3">
            <Capability
              icon={<FileUp aria-hidden="true" size={17} />}
              label="Upload"
              enabled={workspace.can_upload}
            />
            <Capability
              icon={<Sparkles aria-hidden="true" size={17} />}
              label="Context"
              enabled={workspace.can_search_context}
            />
            <Capability
              icon={<Users aria-hidden="true" size={17} />}
              label="Members"
              enabled={canManage}
            />
          </section>

          <section className="border-b border-stone-200 px-5 py-6 sm:px-7">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="section-eyebrow">Your access</p>
                <h3 className="mt-1 text-lg font-semibold text-stone-950">
                  {isAdmin ? 'Platform administrator' : workspace.membership_role ?? 'Catalog only'}
                </h3>
              </div>
              {workspace.membership_role === null ? null : (
                <span className="role-badge">{workspace.membership_role}</span>
              )}
            </div>
            <p className="mt-3 text-sm leading-6 text-stone-600">
              {isAdmin
                ? 'Administration permits membership changes, but does not grant document or context access without a workspace membership.'
                : workspace.membership_role === null
                  ? 'You can discover this workspace, but its documents and context remain inaccessible.'
                  : 'Capabilities are derived from your current workspace membership and update immediately when the policy changes.'}
            </p>
          </section>

          {canManage ? (
            <>
              <section className="border-b border-stone-200 px-5 py-6 sm:px-7">
                <p className="section-eyebrow">Add a member</p>
                <form className="mt-3 flex gap-2" role="search" onSubmit={searchCandidates}>
                  <div className="relative min-w-0 flex-1">
                    <Search className="field-icon" aria-hidden="true" size={16} />
                    <input
                      className="field-input h-10 pl-10"
                      value={candidateSearch}
                      aria-label="Search user candidates"
                      placeholder="Search username"
                      onChange={(event) => setCandidateSearch(event.target.value)}
                    />
                  </div>
                  <button type="submit" className="button-secondary px-4" disabled={searching}>
                    {searching ? 'Searching…' : 'Search'}
                  </button>
                </form>
                {candidates.length === 0 ? null : (
                  <form className="mt-3 grid gap-2 sm:grid-cols-[1fr_9rem_auto]" onSubmit={addMember}>
                    <select
                      className="field-input h-10"
                      aria-label="Candidate user"
                      required
                      value={selectedCandidateId}
                      onChange={(event) => setSelectedCandidateId(event.target.value)}
                    >
                      <option value="">Select a user</option>
                      {candidates.map((candidate) => (
                        <option key={candidate.id} value={candidate.id}>{candidate.username}</option>
                      ))}
                    </select>
                    <select
                      className="field-input h-10"
                      aria-label="Workspace role"
                      value={newRole}
                      onChange={(event) => setNewRole(event.target.value as WorkspaceRole)}
                    >
                      {isAdmin ? <option value="owner">Owner</option> : null}
                      <option value="editor">Editor</option>
                      <option value="viewer">Viewer</option>
                    </select>
                    <button type="submit" className="button-primary h-10 px-4" disabled={adding || selectedCandidateId === ''}>
                      <UserPlus aria-hidden="true" size={16} />
                      {adding ? 'Adding…' : 'Add'}
                    </button>
                  </form>
                )}
              </section>

              <section className="px-5 py-6 sm:px-7">
                <div className="mb-4 flex items-center justify-between gap-4">
                  <div>
                    <p className="section-eyebrow">Access policy</p>
                    <h3 className="mt-1 text-lg font-semibold text-stone-950">Workspace members</h3>
                  </div>
                  <span className="font-mono text-xs text-stone-500">{members.length} total</span>
                </div>
                {error === null ? null : <p className="form-error mb-4" role="alert">{error}</p>}
                {loadingMembers ? (
                  <div className="space-y-2" aria-label="Loading workspace members">
                    {Array.from({ length: 3 }, (_, index) => <div key={index} className="h-16 animate-pulse bg-stone-100" />)}
                  </div>
                ) : members.length === 0 ? (
                  <div className="border border-dashed border-stone-300 px-5 py-8 text-center text-sm text-stone-500">
                    No workspace members.
                  </div>
                ) : (
                  <div className="divide-y divide-stone-200 border-y border-stone-200">
                    {members.map((member) => {
                      const ownerLocked = !isAdmin && member.role === 'owner'
                      return (
                        <div key={member.user_id} className="grid gap-3 py-4 sm:grid-cols-[1fr_9rem_auto] sm:items-center">
                          <div className="min-w-0">
                            <p className="truncate font-medium text-stone-950">{member.username}</p>
                            <p className="mt-0.5 font-mono text-[0.62rem] text-stone-400">{member.user_id.slice(0, 8)}</p>
                          </div>
                          <select
                            className="field-input h-9 text-sm"
                            aria-label={`Role for ${member.username}`}
                            value={member.role}
                            disabled={ownerLocked || updatingUserId === member.user_id}
                            onChange={(event) => changeRole(member, event.target.value as WorkspaceRole)}
                          >
                            {isAdmin || member.role === 'owner' ? <option value="owner">Owner</option> : null}
                            <option value="editor">Editor</option>
                            <option value="viewer">Viewer</option>
                          </select>
                          <button
                            type="button"
                            className="icon-button justify-self-end text-red-700 disabled:text-stone-300"
                            aria-label={`Remove ${member.username}`}
                            disabled={ownerLocked}
                            onClick={() => setRemoveTarget(member)}
                          >
                            <Trash2 aria-hidden="true" size={16} />
                          </button>
                        </div>
                      )
                    })}
                  </div>
                )}
              </section>
            </>
          ) : (
            <section className="px-5 py-8 sm:px-7">
              <div className="border border-dashed border-stone-300 bg-stone-50 px-5 py-8 text-center">
                <CircleSlash2 className="mx-auto text-stone-400" aria-hidden="true" size={24} />
                <h3 className="mt-3 font-semibold text-stone-950">Membership management unavailable</h3>
                <p className="mx-auto mt-2 max-w-sm text-sm leading-6 text-stone-500">
                  Only workspace owners and platform administrators can view or change this access policy.
                </p>
              </div>
            </section>
          )}
        </div>
      </Dialog>
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

interface CapabilityProps {
  icon: React.ReactNode
  label: string
  enabled: boolean
}

function Capability({ icon, label, enabled }: CapabilityProps) {
  return (
    <div className="border-r border-stone-200 px-4 py-4 last:border-r-0">
      <div className={enabled ? 'text-emerald-800' : 'text-stone-300'}>{icon}</div>
      <p className="mt-2 text-xs font-medium text-stone-700">{label}</p>
      <p className="mt-0.5 flex items-center gap-1 text-[0.68rem] text-stone-500">
        {enabled ? <CheckCircle2 aria-hidden="true" size={11} /> : <CircleSlash2 aria-hidden="true" size={11} />}
        {enabled ? 'Allowed' : 'Not allowed'}
      </p>
    </div>
  )
}
