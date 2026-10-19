import { useState, type FormEvent } from 'react'
import { Plus, RotateCcw, UserRound } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { AdminUser, OneTimeCredential } from '../types'
import { ConfirmDialog } from './Dialog'
import { CreateUserDialog } from './CreateUserDialog'
import { DataTable } from './ui/DataTable'
import { EmptyState } from './ui/Feedback'
import { Panel, PanelHeader } from './ui/Panel'
import { SearchField } from './ui/SearchField'
import { useApiResource } from '../hooks/useApiResource'

interface AdminUserPanelProps {
  token: string
  onCredential: (credential: OneTimeCredential) => void
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
}

export function AdminUserPanel({
  token,
  onCredential,
  onUnauthorized,
  onNotice,
}: AdminUserPanelProps) {
  const [search, setSearch] = useState('')
  const [appliedSearch, setAppliedSearch] = useState('')
  const [users, setUsers] = useState<AdminUser[]>([])
  const [total, setTotal] = useState(0)
  const [loadingMore, setLoadingMore] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null)
  const [resetting, setResetting] = useState(false)

  const directory = useApiResource(
    (signal) => api.listAdminUsers(token, { search: appliedSearch, signal }),
    [token, appliedSearch],
    { onUnauthorized },
  )

  function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setAppliedSearch(search.trim())
  }

  async function resetPassword() {
    if (resetTarget === null) return
    setResetting(true)
    try {
      const result = await api.resetPassword(token, resetTarget.id)
      onCredential({
        username: resetTarget.username,
        password: result.initial_password,
        reason: 'reset',
      })
      setResetTarget(null)
      onNotice(`Password reset for ${resetTarget.username}.`)
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setResetting(false)
    }
  }

  async function loadMore() {
    setLoadingMore(true)
    try {
      const result = await api.listAdminUsers(token, {
        search: appliedSearch,
        offset: users.length,
      })
      setUsers((current) => [...current, ...result.items])
      setTotal(result.total)
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setLoadingMore(false)
    }
  }

  const rows = users.length > 0 ? users : (directory.data?.items ?? [])
  const known = users.length > 0 ? total : (directory.data?.total ?? 0)

  return (
    <Panel>
      <PanelHeader
        id="user-directory-title"
        eyebrow="Platform administration"
        title="User directory"
        actions={
          <button
            type="button"
            className="btn btn-primary shrink-0"
            onClick={() => setCreateOpen(true)}
          >
            <Plus aria-hidden="true" size={17} />
            New user
          </button>
        }
      />

      <div className="border-b border-line px-5 py-3.5">
        <form className="flex max-w-md gap-2" role="search" onSubmit={handleSearch}>
          <SearchField
            id="user-search"
            label="Search users"
            value={search}
            onChange={setSearch}
            placeholder="Search username"
            className="min-w-0 flex-1"
          />
          <button type="submit" className="btn btn-secondary">
            Search
          </button>
        </form>
      </div>

      {directory.error === null ? null : (
        <p className="error-note m-5" role="alert">
          {directory.error}
        </p>
      )}

      {directory.loading && rows.length === 0 ? (
        <div className="space-y-2 px-5 py-6" aria-hidden="true">
          {Array.from({ length: 3 }, (_, index) => (
            <div key={index} className="h-10 animate-pulse rounded-card bg-sunken" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<UserRound aria-hidden="true" size={20} />}
          title="No users match this search"
          description="Try a different username, or clear the search to list everyone."
        />
      ) : (
        <DataTable
          caption="User directory"
          rows={rows}
          rowKey={(user) => user.id}
          columns={[
            {
              key: 'user',
              header: 'User',
              render: (user) => (
                <div className="flex items-center gap-3">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-card border border-line bg-sunken text-ink-muted">
                    <UserRound aria-hidden="true" size={15} />
                  </span>
                  <div className="min-w-0">
                    <p className="font-medium text-ink">{user.username}</p>
                    <p className="mt-0.5 font-mono text-meta text-ink-faint">
                      {user.id.slice(0, 8)}
                    </p>
                  </div>
                </div>
              ),
            },
            {
              key: 'role',
              header: 'Platform role',
              render: (user) => <span className="badge">{user.role}</span>,
            },
            {
              key: 'status',
              header: 'Status',
              render: (user) => (
                <span className="inline-flex items-center gap-1.5">
                  <span
                    className={
                      user.is_active ? 'h-1.5 w-1.5 rounded-full bg-success' : 'h-1.5 w-1.5 rounded-full bg-ink-faint'
                    }
                    aria-hidden="true"
                  />
                  {user.is_active ? 'Active' : 'Inactive'}
                </span>
              ),
            },
            {
              key: 'action',
              header: 'Action',
              align: 'right',
              render: (user) => (
                <button
                  type="button"
                  className="btn btn-quiet"
                  onClick={() => setResetTarget(user)}
                >
                  <RotateCcw aria-hidden="true" size={15} />
                  Reset password
                </button>
              ),
            },
          ]}
        />
      )}

      <footer className="flex items-center justify-between gap-4 border-t border-line px-5 py-2.5 font-mono text-meta text-ink-faint">
        <span>
          {rows.length} of {known} {known === 1 ? 'user' : 'users'}
        </span>
        {rows.length < known ? (
          <button
            type="button"
            className="btn btn-quiet font-sans"
            disabled={loadingMore}
            onClick={loadMore}
          >
            {loadingMore ? 'Loading…' : 'Load more'}
          </button>
        ) : null}
      </footer>

      <CreateUserDialog
        open={createOpen}
        token={token}
        onClose={() => setCreateOpen(false)}
        onUnauthorized={onUnauthorized}
        onCreated={(credential) => {
          setCreateOpen(false)
          onCredential(credential)
          directory.reload()
        }}
      />
      <ConfirmDialog
        open={resetTarget !== null}
        title="Reset this password?"
        description={`A new permanent password will replace the current one for ${resetTarget?.username ?? 'this user'}, and all existing sessions will be invalidated.`}
        confirmLabel="Generate new password"
        busy={resetting}
        destructive
        onClose={() => setResetTarget(null)}
        onConfirm={resetPassword}
      />
    </Panel>
  )
}
