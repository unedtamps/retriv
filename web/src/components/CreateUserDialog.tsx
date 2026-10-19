import { useState, type FormEvent } from 'react'
import { KeyRound } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { OneTimeCredential } from '../types'
import { Dialog, DialogActions } from './Dialog'

interface CreateUserDialogProps {
  open: boolean
  token: string
  onCreated: (credential: OneTimeCredential) => void
  onUnauthorized: () => void
  onClose: () => void
}

/**
 * Creating a user.
 *
 * This lived inside the user directory's file, which made that file two
 * things at once. Every other dialog in the app has its own file.
 */
export function CreateUserDialog({
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
      description="Retriv generates a permanent password and displays it once after creation."
      onClose={onClose}
    >
      <form onSubmit={handleSubmit}>
        <div className="space-y-5 px-5 py-5 sm:px-6">
          <div>
            <label className="label" htmlFor="new-username">
              Username
            </label>
            <input
              id="new-username"
              className="field"
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
            <legend className="label">Platform role</legend>
            <div className="grid grid-cols-2 gap-3">
              {(['user', 'admin'] as const).map((value) => (
                <label
                  key={value}
                  className={role === value ? 'choice choice-active' : 'choice'}
                >
                  <input
                    type="radio"
                    name="platform-role"
                    value={value}
                    checked={role === value}
                    onChange={() => setRole(value)}
                    className="mt-0.5 accent-accent-ink"
                  />
                  <span className="font-medium capitalize text-ink">{value}</span>
                  <span className="text-xs leading-5 text-ink-muted">
                    {value === 'admin'
                      ? 'Can use platform administration.'
                      : 'Uses assigned workspaces.'}
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
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
            disabled={status === 'submitting'}
          >
            <KeyRound aria-hidden="true" size={17} />
            {status === 'submitting' ? 'Creating…' : 'Create user'}
          </button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
