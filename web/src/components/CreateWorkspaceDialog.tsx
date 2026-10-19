import { useEffect, useState, type FormEvent } from 'react'
import { Building2 } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { AdminUser, Workspace } from '../types'
import { Dialog, DialogActions } from './Dialog'
import { SearchField } from './ui/SearchField'

interface CreateWorkspaceDialogProps {
  open: boolean
  token: string
  onCreated: (workspace: Workspace) => void
  onUnauthorized: () => void
  onClose: () => void
}

export function CreateWorkspaceDialog({
  open,
  token,
  onCreated,
  onUnauthorized,
  onClose,
}: CreateWorkspaceDialogProps) {
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [slugEdited, setSlugEdited] = useState(false)
  const [description, setDescription] = useState('')
  const [ownerId, setOwnerId] = useState('')
  const [users, setUsers] = useState<AdminUser[]>([])
  const [userSearch, setUserSearch] = useState('')
  const [loadingUsers, setLoadingUsers] = useState(true)
  const [status, setStatus] = useState<'idle' | 'submitting'>('idle')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    api
      .listAdminUsers(token, { signal: controller.signal })
      .then((result) => {
        setUsers(result.items.filter((user) => user.is_active))
        setLoadingUsers(false)
      })
      .catch((caught: unknown) => {
        if (caught instanceof Error && caught.name === 'AbortError') return
        if (isUnauthorized(caught)) onUnauthorized()
        else setError(getErrorMessage(caught))
        setLoadingUsers(false)
      })
    return () => controller.abort()
  }, [open, token, onUnauthorized])

  async function searchUsers() {
    setLoadingUsers(true)
    try {
      const result = await api.listAdminUsers(token, { search: userSearch })
      setUsers(result.items.filter((user) => user.is_active))
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else setError(getErrorMessage(caught))
    } finally {
      setLoadingUsers(false)
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setStatus('submitting')
    setError(null)
    try {
      const workspace = await api.createWorkspace(token, {
        name,
        slug,
        description: description.trim() === '' ? null : description,
        owner_user_id: ownerId,
      })
      setName('')
      setSlug('')
      setSlugEdited(false)
      setDescription('')
      setOwnerId('')
      setStatus('idle')
      onCreated(workspace)
    } catch (caught) {
      if (isUnauthorized(caught)) {
        onUnauthorized()
        return
      }
      setError(getErrorMessage(caught))
      setStatus('idle')
    }
  }

  function updateName(value: string) {
    setName(value)
    if (!slugEdited) setSlug(toSlug(value))
  }

  return (
    <Dialog
      open={open}
      title="Create a workspace"
      eyebrow="Platform admin"
      description="A workspace must begin with an owner. Ownership can only be assigned by an administrator."
      onClose={onClose}
    >
      <form onSubmit={handleSubmit}>
        <div className="space-y-5 px-5 py-5 sm:px-6">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="workspace-name">
                Name
              </label>
              <input
                id="workspace-name"
                className="field"
                value={name}
                autoFocus
                required
                maxLength={255}
                placeholder="Finance operations"
                onChange={(event) => updateName(event.target.value)}
              />
            </div>
            <div>
              <label className="label" htmlFor="workspace-slug">
                Slug
              </label>
              <input
                id="workspace-slug"
                className="field font-mono text-sm"
                value={slug}
                required
                maxLength={100}
                pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
                placeholder="finance-operations"
                onChange={(event) => {
                  setSlugEdited(true)
                  setSlug(event.target.value.toLowerCase())
                }}
              />
            </div>
          </div>
          <div>
            <label className="label" htmlFor="workspace-description">
              Description <span className="font-normal text-ink-faint">Optional</span>
            </label>
            <textarea
              id="workspace-description"
              className="field min-h-24 resize-y"
              value={description}
              maxLength={1000}
              placeholder="What context and documents belong in this workspace?"
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>
          <div>
            <div className="mb-2 flex items-end justify-between gap-4">
              <label className="label mb-0" htmlFor="workspace-owner">
                Initial owner
              </label>
              <span className="text-xs text-ink-faint">Active users only</span>
            </div>
            <div className="mb-2 flex gap-2" role="search">
              <SearchField
                id="workspace-owner-search"
                label="Search owner candidates"
                value={userSearch}
                onChange={setUserSearch}
                placeholder="Filter users"
                className="min-w-0 flex-1"
              />
              <button type="button" className="btn btn-secondary" onClick={searchUsers}>
                Search
              </button>
            </div>
            <select
              id="workspace-owner"
              className="field"
              value={ownerId}
              required
              disabled={loadingUsers}
              onChange={(event) => setOwnerId(event.target.value)}
            >
              <option value="">
                {loadingUsers ? 'Loading users…' : 'Select an owner'}
              </option>
              {users.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.username} · {user.role}
                </option>
              ))}
            </select>
          </div>
          {error === null ? null : (
            <p className="error-note" role="alert">
              {error}
            </p>
          )}
        </div>
        <DialogActions>
          <button
            type="button"
            className="btn btn-secondary"
            disabled={status === 'submitting'}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={status === 'submitting' || ownerId === ''}
          >
            <Building2 aria-hidden="true" size={17} />
            {status === 'submitting' ? 'Creating…' : 'Create workspace'}
          </button>
        </DialogActions>
      </form>
    </Dialog>
  )
}

function toSlug(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
}
