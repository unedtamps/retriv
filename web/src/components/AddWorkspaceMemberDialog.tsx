import { useEffect, useMemo, useState } from 'react'
import { UserPlus } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { AdminUser, AdminWorkspace, WorkspaceMember, WorkspaceRole } from '../types'
import { Dialog } from './Dialog'
import { RoleTiles } from './ui/RoleTiles'
import { UserSearchRoster } from './ui/UserSearchRoster'

interface AddWorkspaceMemberDialogProps {
  open: boolean
  workspace: AdminWorkspace
  onUnauthorized: () => void
  onAdded: (member: WorkspaceMember) => void
  onClose: () => void
}

const ROLE_CHOICES: WorkspaceRole[] = ['owner', 'editor', 'viewer']

/**
 * Grant a platform user a role in one workspace, as the administrator.
 *
 * Candidates come from the platform user directory rather than the
 * owner-facing member-candidate search, which answers 403 to a caller with no
 * membership. The directory refilters while the query is typed, and users
 * already in the workspace are withheld from the list, so the PUT can only
 * ever add.
 */
export function AddWorkspaceMemberDialog({
  open,
  workspace,
  onUnauthorized,
  onAdded,
  onClose,
}: AddWorkspaceMemberDialogProps) {
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [loadedQuery, setLoadedQuery] = useState<string | null>(null)
  const [users, setUsers] = useState<AdminUser[]>([])
  const [total, setTotal] = useState(0)
  const [selectedUserId, setSelectedUserId] = useState('')
  const [role, setRole] = useState<WorkspaceRole>('viewer')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    const controller = new AbortController()
    api
      .listAdminUsers({ search: debouncedSearch, signal: controller.signal })
      .then((result) => {
        setUsers(result.items.filter((user) => user.is_active))
        setTotal(result.total)
        setLoadedQuery(debouncedSearch)
      })
      .catch((caught: unknown) => {
        if (caught instanceof Error && caught.name === 'AbortError') return
        setLoadedQuery(debouncedSearch)
        if (isUnauthorized(caught)) onUnauthorized()
        else setError(getErrorMessage(caught))
      })
    return () => controller.abort()
  }, [debouncedSearch, onUnauthorized])

  const existingIds = useMemo(
    () => new Set(workspace.members.map((member) => member.user_id)),
    [workspace.members],
  )
  const candidates = users.filter(
    (user) => user.role !== 'admin' && !existingIds.has(user.id),
  )
  const searching = loadedQuery !== debouncedSearch
  const selectedUser = users.find((user) => user.id === selectedUserId)

  async function handleAdd() {
    if (selectedUserId === '') return
    setSubmitting(true)
    setError(null)
    try {
      const member = await api.setMemberRole(workspace.id, selectedUserId, role, true)
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
          id="member-candidate-search"
          label="Search platform users"
          search={search}
          onSearchChange={setSearch}
          candidates={candidates}
          searching={searching}
          selectedId={selectedUserId}
          onSelect={setSelectedUserId}
          emptyMessage="No matching active user, or everyone found is already a member."
          total={total}
        />

        <RoleTiles
          label="Workspace role"
          labelId="member-role-label"
          value={role}
          onChange={setRole}
          roles={ROLE_CHOICES}
        />

        {error === null ? null : (
          <p className="error-note" role="alert">
            {error}
          </p>
        )}
      </div>

      <div className="flex flex-col-reverse gap-2 border-t border-line px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6">
        {selectedUser === undefined ? null : (
          <div className="flex items-center gap-2.5">
            <span className="text-xs text-ink-faint">Granting</span>
            <span className="rounded-stamp bg-sunken px-2 py-1 font-mono text-[11px] font-medium text-ink">
              {selectedUser.username} → {role}
            </span>
          </div>
        )}
        <div className="flex gap-2 sm:ml-auto">
          <button
            type="button"
            className="btn btn-secondary"
            disabled={submitting}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={submitting || selectedUserId === ''}
            onClick={handleAdd}
          >
            <UserPlus aria-hidden="true" size={17} />
            {submitting ? 'Adding…' : 'Add member'}
          </button>
        </div>
      </div>
    </Dialog>
  )
}
