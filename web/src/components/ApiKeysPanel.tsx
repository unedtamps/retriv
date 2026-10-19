import { useState, type FormEvent } from 'react'
import { KeyRound, Plus, Trash2 } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { ApiKeySummary, CreatedApiKey } from '../types'
import { ConfirmDialog, Dialog, DialogActions } from './Dialog'
import { CopyButton, EmptyState, SecretWarning } from './ui/Feedback'
import { Panel, PanelHeader } from './ui/Panel'
import { useApiResource } from '../hooks/useApiResource'

interface ApiKeysPanelProps {
  token: string
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
}

/**
 * API keys are the credential MCP clients send to /mcp. The plaintext is
 * shown once, immediately after creation, and the panel never holds onto it
 * afterwards: it exists in component state only while the dialog is open, so
 * a re-render cannot resurrect it from a stored list.
 */
export function ApiKeysPanel({ token, onUnauthorized, onNotice }: ApiKeysPanelProps) {
  const [createOpen, setCreateOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)
  const [revealed, setRevealed] = useState<CreatedApiKey | null>(null)
  const [revokeTarget, setRevokeTarget] = useState<ApiKeySummary | null>(null)
  const [revoking, setRevoking] = useState(false)

  const keys = useApiResource(
    (signal) => api.listApiKeys(token, signal),
    [token],
    { onUnauthorized },
  )

  async function createKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const name = newName.trim()
    if (name === '') return
    setCreating(true)
    try {
      const created = await api.createApiKey(token, name)
      setCreateOpen(false)
      setNewName('')
      setRevealed(created)
      keys.reload()
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setCreating(false)
    }
  }

  async function revokeKey() {
    if (revokeTarget === null) return
    setRevoking(true)
    try {
      await api.revokeApiKey(token, revokeTarget.id)
      setRevokeTarget(null)
      keys.reload()
      onNotice(`API key "${revokeTarget.name}" revoked. It cannot be used from now on.`)
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setRevoking(false)
    }
  }

  const items = keys.data?.items ?? []
  const activeKeys = items.filter((key) => key.revoked_at === null)
  const revokedKeys = items.filter((key) => key.revoked_at !== null)

  return (
    <Panel>
      <PanelHeader
        id="api-keys-title"
        eyebrow="Programmatic access"
        title="API keys"
        description={
          <>
            A key authenticates MCP clients at <code className="font-mono text-[0.85em]">/mcp</code>.
            It carries your identity, not a fixed scope: your current workspace memberships
            are checked on every call, so access changes take effect immediately.
          </>
        }
        actions={
          <button
            type="button"
            className="btn btn-primary shrink-0"
            onClick={() => setCreateOpen(true)}
          >
            <Plus aria-hidden="true" size={17} />
            New key
          </button>
        }
      />

      {keys.error === null ? null : (
        <p className="border-b border-line px-5 py-3 text-sm text-danger" role="alert">
          {keys.error}
        </p>
      )}

      {keys.loading && items.length === 0 ? (
        <p className="px-5 py-8 text-sm text-ink-muted">Loading keys…</p>
      ) : items.length === 0 ? (
        <EmptyState
          icon={<KeyRound aria-hidden="true" size={20} />}
          title="No API keys yet"
          description="Create one to let an MCP client such as Claude Code or Codex search your workspaces and query CSV files on your behalf."
        />
      ) : (
        <div className="divide-y divide-line">
          {activeKeys.map((key) => (
            <div
              key={key.id}
              className="flex flex-col gap-3 px-5 py-3.5 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-ink">{key.name}</p>
                <p className="mt-0.5 font-mono text-meta text-ink-faint">
                  {key.key_prefix}… · created {formatDate(key.created_at)} ·{' '}
                  {key.last_used_at === null
                    ? 'never used'
                    : `last used ${formatDate(key.last_used_at)}`}
                </p>
              </div>
              <button
                type="button"
                className="btn btn-quiet shrink-0 text-danger hover:text-danger"
                onClick={() => setRevokeTarget(key)}
              >
                <Trash2 aria-hidden="true" size={15} />
                Revoke
              </button>
            </div>
          ))}
          {revokedKeys.length > 0 ? (
            <p className="px-5 py-2.5 font-mono text-meta text-ink-faint">
              {revokedKeys.length} revoked key{revokedKeys.length === 1 ? '' : 's'} retained for
              audit
            </p>
          ) : null}
        </div>
      )}

      <Dialog
        open={createOpen}
        title="Create API key"
        eyebrow="MCP access"
        description="The key is shown once, right after it is created. Give it a name you will recognise later."
        preventBackdropClose={creating}
        onClose={() => setCreateOpen(false)}
      >
        <form onSubmit={createKey}>
          <div className="px-5 py-5 sm:px-6">
            <label className="label" htmlFor="api-key-name">
              Key name
            </label>
            <input
              id="api-key-name"
              className="field"
              value={newName}
              maxLength={100}
              placeholder="laptop · Claude Code · ci-agent"
              autoFocus
              onChange={(event) => setNewName(event.target.value)}
            />
          </div>
          <DialogActions>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={creating}
              onClick={() => setCreateOpen(false)}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={creating || newName.trim() === ''}
            >
              {creating ? 'Creating…' : 'Create key'}
            </button>
          </DialogActions>
        </form>
      </Dialog>

      <Dialog
        open={revealed !== null}
        title="API key created"
        eyebrow="One-time display"
        description="Store this key now. Only its hash is saved, so it cannot be shown again."
        preventBackdropClose
        onClose={() => setRevealed(null)}
      >
        {revealed === null ? null : (
          <div className="px-5 py-5 sm:px-6">
            <SecretWarning>{revealed.warning}</SecretWarning>
            <div className="mt-4 rounded-card border border-line bg-sunken px-4 py-3">
              <p className="text-sm text-ink-muted">Key</p>
              <p className="mt-1 break-all font-mono text-sm font-semibold text-ink">
                {revealed.key}
              </p>
            </div>
            <p className="mt-4 text-sm leading-6 text-ink-muted">
              Send it as{' '}
              <code className="font-mono text-[0.85em]">
                Authorization: Bearer &lt;key&gt;
              </code>{' '}
              when connecting an MCP client to{' '}
              <code className="font-mono text-[0.85em]">/mcp</code>.
            </p>
            <DialogActions>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => setRevealed(null)}
              >
                I have stored it safely
              </button>
              <CopyButton value={revealed.key} label="Copy key" />
            </DialogActions>
          </div>
        )}
      </Dialog>

      <ConfirmDialog
        open={revokeTarget !== null}
        title="Revoke API key"
        description={
          revokeTarget === null
            ? ''
            : `"${revokeTarget.name}" stops working immediately. Any MCP client using it will be rejected on its next request. This cannot be undone.`
        }
        confirmLabel="Revoke key"
        destructive
        busy={revoking}
        onConfirm={revokeKey}
        onClose={() => setRevokeTarget(null)}
      />
    </Panel>
  )
}

function formatDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}
