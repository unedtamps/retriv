import { useEffect, useState, type FormEvent } from 'react'
import { KeyRound, Plus, RotateCcw, Search, ShieldCheck, UserRound } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { AdminUser, OneTimeCredential } from '../types'
import { ConfirmDialog, Dialog } from './Dialog'

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
  const [users, setUsers] = useState<AdminUser[]>([])
  const [total, setTotal] = useState(0)
  const [search, setSearch] = useState('')
  const [appliedSearch, setAppliedSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [createOpen, setCreateOpen] = useState(false)
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null)
  const [resetting, setResetting] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    api
      .listAdminUsers(token, { search: appliedSearch, signal: controller.signal })
      .then((result) => {
        setUsers(result.items)
        setTotal(result.total)
        setLoading(false)
      })
      .catch((caught: unknown) => {
        if (caught instanceof Error && caught.name === 'AbortError') return
        if (isUnauthorized(caught)) {
          onUnauthorized()
          return
        }
        setError(getErrorMessage(caught))
        setLoading(false)
      })
    return () => controller.abort()
  }, [token, appliedSearch, refreshKey, onUnauthorized])

  function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setLoading(true)
    setError(null)
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
    setError(null)
    try {
      const result = await api.listAdminUsers(token, {
        search: appliedSearch,
        offset: users.length,
      })
      setUsers((current) => [...current, ...result.items])
      setTotal(result.total)
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else setError(getErrorMessage(caught))
    } finally {
      setLoadingMore(false)
    }
  }

  return (
    <section className="border border-stone-300 bg-white" aria-labelledby="user-directory-title">
      <header className="flex flex-col gap-4 border-b border-stone-200 px-5 py-5 sm:flex-row sm:items-center sm:justify-between lg:px-6">
        <div>
          <div className="flex items-center gap-2 text-emerald-900">
            <ShieldCheck aria-hidden="true" size={17} strokeWidth={1.8} />
            <p className="font-mono text-[0.65rem] font-semibold uppercase tracking-[0.17em]">
              Platform administration
            </p>
          </div>
          <h2 id="user-directory-title" className="mt-2 text-xl font-semibold tracking-[-0.02em] text-stone-950">
            User directory
          </h2>
        </div>
        <button type="button" className="button-primary" onClick={() => setCreateOpen(true)}>
          <Plus aria-hidden="true" size={17} />
          New user
        </button>
      </header>

      <div className="border-b border-stone-200 px-5 py-4 lg:px-6">
        <form className="flex max-w-md gap-2" role="search" onSubmit={handleSearch}>
          <label className="sr-only" htmlFor="user-search">
            Search users
          </label>
          <div className="relative min-w-0 flex-1">
            <Search className="field-icon" aria-hidden="true" size={16} />
            <input
              id="user-search"
              className="field-input h-10 pl-10"
              value={search}
              placeholder="Search username"
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <button type="submit" className="button-secondary px-4">
            Search
          </button>
        </form>
      </div>

      {error === null ? null : (
        <div className="m-5 border-l-2 border-red-700 bg-red-50 px-4 py-3 text-sm text-red-900" role="alert">
          {error}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="data-table min-w-[42rem]">
          <thead>
            <tr>
              <th>User</th>
              <th>Platform role</th>
              <th>Status</th>
              <th className="text-right">Action</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              Array.from({ length: 3 }, (_, index) => (
                <tr key={index} aria-hidden="true">
                  <td colSpan={4}><div className="h-5 w-full animate-pulse bg-stone-100" /></td>
                </tr>
              ))
            ) : users.length === 0 ? (
              <tr>
                <td colSpan={4} className="py-10 text-center text-sm text-stone-500">
                  No users match this search.
                </td>
              </tr>
            ) : (
              users.map((user) => (
                <tr key={user.id}>
                  <td>
                    <div className="flex items-center gap-3">
                      <span className="flex h-8 w-8 items-center justify-center border border-stone-200 bg-stone-50 text-stone-600">
                        <UserRound aria-hidden="true" size={15} />
                      </span>
                      <div>
                        <p className="font-medium text-stone-950">{user.username}</p>
                        <p className="mt-0.5 font-mono text-[0.62rem] text-stone-400">{user.id.slice(0, 8)}</p>
                      </div>
                    </div>
                  </td>
                  <td><span className="role-badge">{user.role}</span></td>
                  <td>
                    <span className={user.is_active ? 'status-dot status-active' : 'status-dot'}>
                      {user.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td className="text-right">
                    <button
                      type="button"
                      className="text-button ml-auto"
                      onClick={() => setResetTarget(user)}
                    >
                      <RotateCcw aria-hidden="true" size={15} />
                      Reset password
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <footer className="flex items-center justify-between gap-4 border-t border-stone-200 px-5 py-3 font-mono text-[0.65rem] uppercase tracking-[0.12em] text-stone-500 lg:px-6">
        <span>{users.length} of {total} {total === 1 ? 'user' : 'users'}</span>
        {users.length < total ? (
          <button type="button" className="text-button font-sans normal-case tracking-normal" disabled={loadingMore} onClick={loadMore}>
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
          setLoading(true)
          setRefreshKey((value) => value + 1)
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
    </section>
  )
}

interface CreateUserDialogProps {
  open: boolean
  token: string
  onCreated: (credential: OneTimeCredential) => void
  onUnauthorized: () => void
  onClose: () => void
}

function CreateUserDialog({
  open,
  token,
  onCreated,
  onUnauthorized,
  onClose,
}: CreateUserDialogProps) {
  const [username, setUsername] = useState('')
  const [role, setRole] = useState<'admin' | 'user'>('user')
  const [status, setStatus] = useState<'idle' | 'submitting'>('idle')
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setStatus('submitting')
    setError(null)
    try {
      const created = await api.createUser(token, username, role)
      setUsername('')
      setRole('user')
      setStatus('idle')
      onCreated({
        username: created.username,
        password: created.initial_password,
        reason: 'created',
      })
    } catch (caught) {
      if (isUnauthorized(caught)) {
        onUnauthorized()
        return
      }
      setError(getErrorMessage(caught))
      setStatus('idle')
    }
  }

  return (
    <Dialog
      open={open}
      title="Create a user"
      eyebrow="Platform admin"
      description="Kerjakan generates a permanent password and displays it once after creation."
      onClose={onClose}
    >
      <form className="space-y-5 px-5 py-6 sm:px-7" onSubmit={handleSubmit}>
        <div>
          <label className="field-label" htmlFor="new-username">Username</label>
          <input
            id="new-username"
            className="field-input"
            value={username}
            autoComplete="off"
            autoFocus
            maxLength={255}
            required
            placeholder="e.g. nadia"
            onChange={(event) => setUsername(event.target.value)}
          />
        </div>
        <fieldset>
          <legend className="field-label">Platform role</legend>
          <div className="grid grid-cols-2 gap-3">
            {(['user', 'admin'] as const).map((value) => (
              <label key={value} className={role === value ? 'choice-card choice-card-active' : 'choice-card'}>
                <input
                  type="radio"
                  name="platform-role"
                  value={value}
                  checked={role === value}
                  onChange={() => setRole(value)}
                />
                <span className="font-medium capitalize text-stone-950">{value}</span>
                <span className="text-xs leading-5 text-stone-500">
                  {value === 'admin' ? 'Can use platform administration.' : 'Uses assigned workspaces.'}
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        {error === null ? null : <p className="form-error" role="alert">{error}</p>}
        <div className="flex flex-col-reverse gap-3 border-t border-stone-200 pt-5 sm:flex-row sm:justify-end">
          <button type="button" className="button-secondary" disabled={status === 'submitting'} onClick={onClose}>Cancel</button>
          <button type="submit" className="button-primary" disabled={status === 'submitting'}>
            <KeyRound aria-hidden="true" size={17} />
            {status === 'submitting' ? 'Creating…' : 'Create user'}
          </button>
        </div>
      </form>
    </Dialog>
  )
}
