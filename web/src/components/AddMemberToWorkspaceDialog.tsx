import { useEffect, useMemo, useState } from 'react'
import { UserPlus } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { Workspace, WorkspaceMember, WorkspaceRole } from '../types'
import { Dialog } from './Dialog'
import { RoleTiles } from './ui/RoleTiles'
import { UserSearchRoster, type UserRosterEntry } from './ui/UserSearchRoster'

interface AddWorkspaceMemberDialogProps {
  open: boolean
  workspace: Workspace
  isAdmin: boolean
  existingMemberIds: string[]
  onUnauthorized: () => void
  onAdded: (member: WorkspaceMember) => void
  onClose: () => void
}

/**
 * Grant an existing platform user a role in this workspace.
 *
 * It lives in a dialog rather than in the page: adding a member is an
 * occasional act, and a permanent search box plus role picker plus button
 * pushed the actual member list most of a screen down.
 *
 * Candidates come from the platform user directory for an admin (who holds
 * no membership to ask the owner-facing search) and from the workspace's own
 * candidate search for an owner. Users already in the workspace are withheld,
 * so the PUT can only ever add.
 */
export function AddWorkspaceMemberDialog({
  open,
  workspace,
  isAdmin,
  existingMemberIds,
  onUnauthorized,
  onAdded,
  onClose,
}: AddWorkspaceMemberDialogProps) {
  const [search, setSearch] = useState('')
  const [debounced, setDebounced] = useState('')
  const [loadedQuery, setLoadedQuery] = useState<string | null>(null)
  const [entries, setEntries] = useState<UserRosterEntry[]>([])
  const [total, setTotal] = useState(0)
  const [selectedId, setSelectedId] = useState('')
  const [role, setRole] = useState<WorkspaceRole>('viewer')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(search.trim()), 420)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    const controller = new AbortController()
    const load = async () => {
      if (isAdmin) {
        const result = await api.listAdminUsers({
          search: debounced,
          signal: controller.signal,
        })
        setEntries(
          result.items
            .filter((user) => user.is_active && user.role !== 'admin')
            .map((user) => ({ id: user.id, username: user.username, role: user.role })),
        )
        setTotal(result.total)
      } else if (debounced.length < 2) {
        setEntries([])
        setTotal(0)
      } else {
        const result = await api.searchMemberCandidates(workspace.id, debounced)
        setEntries(result.items)
        setTotal(result.items.length)
      }
      setLoadedQuery(debounced)
    }
    load().catch((caught: unknown) => {
      if (caught instanceof Error && caught.name === 'AbortError') return
      setLoadedQuery(debounced)
      if (isUnauthorized(caught)) onUnauthorized()
      else setError(getErrorMessage(caught))
    })
    return () => controller.abort()
  }, [debounced, isAdmin, workspace.id, onUnauthorized])

  const existing = useMemo(() => new Set(existingMemberIds), [existingMemberIds])
  const candidates = entries.filter((entry) => !existing.has(entry.id))
  const searching = loadedQuery !== debounced
  const selected = entries.find((entry) => entry.id === selectedId)

  const emptyHint =
    !isAdmin && debounced.length < 2
      ? 'Type at least two characters to search for a user.'
      : 'No matching user.'

  async function handleAdd() {
    if (selectedId === '') return
    setSubmitting(true)
    setError(null)
    try {
      const member = await api.setMemberRole(workspace.id, selectedId, role, isAdmin)
      setSubmitting(false)
      onAdded(member)
    } catch (caught) {
      if (isUnauthorized(caught)) {
        onUnauthorized()
        return
      }
      setError(getErrorMessage(caught))
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open={open}
      eyebrow={workspace.name}
      title="Add a member"
      description="Grant an existing platform user a role in this workspace. The change applies immediately."
      onClose={onClose}
    >
      <div className="space-y-5 px-5 py-5 sm:px-6">
        <UserSearchRoster
          id="add-member-search"
          label="Search users"
          search={search}
          onSearchChange={setSearch}
          candidates={candidates}
          searching={searching}
          selectedId={selectedId}
          onSelect={setSelectedId}
          emptyMessage={emptyHint}
          total={total}
        />
        <RoleTiles
          label="Workspace role"
          labelId="add-member-role-label"
          value={role}
          onChange={setRole}
          roles={isAdmin ? ['owner', 'editor', 'viewer'] : ['editor', 'viewer']}
        />
        {error === null ? null : (
          <p className="error-note" role="alert">
            {error}
          </p>
        )}
      </div>
      <div className="flex flex-col-reverse gap-2 border-t border-line px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6">
        {selected === undefined ? null : (
          <div className="flex items-center gap-2.5">
            <span className="text-xs text-ink-faint">Granting</span>
            <span className="rounded-stamp bg-sunken px-2 py-1 font-mono text-[11px] font-medium text-ink">
              {selected.username} → {role}
            </span>
          </div>
        )}
        <div className="flex gap-2 sm:ml-auto">
          <button type="button" className="btn btn-secondary" disabled={submitting} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={submitting || selectedId === ''}
            onClick={handleAdd}
          >
            <UserPlus aria-hidden="true" size={16} />
            {submitting ? 'Adding…' : 'Add member'}
          </button>
        </div>
      </div>
    </Dialog>
  )
}
