import { useEffect, useState, type FormEvent } from 'react'
import { Check, Copy, KeyRound, Plus, Trash2 } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { ApiKeySummary, CreatedApiKey } from '../types'
import { ConfirmDialog, Dialog } from './Dialog'

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
  const [keys, setKeys] = useState<ApiKeySummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [createOpen, setCreateOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)
  const [revealed, setRevealed] = useState<CreatedApiKey | null>(null)
  const [copied, setCopied] = useState(false)
  const [revokeTarget, setRevokeTarget] = useState<ApiKeySummary | null>(null)
  const [revoking, setRevoking] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    api
      .listApiKeys(token, controller.signal)
      .then((result) => {
        setKeys(result.items)
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
  }, [token, refreshKey, onUnauthorized])

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
      setCopied(false)
      setRefreshKey((key) => key + 1)
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setCreating(false)
    }
  }

  async function copyKey() {
    if (revealed === null) return
    await navigator.clipboard.writeText(revealed.key)
    setCopied(true)
  }

  async function revokeKey() {
    if (revokeTarget === null) return
    setRevoking(true)
    try {
      await api.revokeApiKey(token, revokeTarget.id)
      setRevokeTarget(null)
      setRefreshKey((key) => key + 1)
      onNotice(`API key "${revokeTarget.name}" revoked. It cannot be used from now on.`)
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setRevoking(false)
    }
  }

  const activeKeys = keys.filter((key) => key.revoked_at === null)
  const revokedKeys = keys.filter((key) => key.revoked_at !== null)

  return (
    <section className="border border-stone-300 bg-white" aria-labelledby="api-keys-title">
      <header className="flex flex-col gap-4 border-b border-stone-200 px-5 py-5 sm:flex-row sm:items-center sm:justify-between lg:px-6">
        <div>
          <div className="flex items-center gap-2 text-emerald-900">
            <KeyRound aria-hidden="true" size={17} strokeWidth={1.8} />
            <p className="font-mono text-[0.65rem] font-semibold uppercase tracking-[0.17em]">
              Programmatic access
            </p>
          </div>
          <h2 id="api-keys-title" className="mt-2 text-xl font-semibold tracking-[-0.02em] text-stone-950">
            API keys
          </h2>
          <p className="mt-1.5 max-w-2xl text-sm leading-6 text-stone-600">
            A key authenticates MCP clients at <code className="font-mono text-[0.85em]">/mcp</code>.
            It carries your identity, not a fixed scope: your current workspace
            memberships are checked on every call, so access changes take effect immediately.
          </p>
        </div>
        <button type="button" className="button-primary shrink-0" onClick={() => setCreateOpen(true)}>
          <Plus aria-hidden="true" size={17} />
          New key
        </button>
      </header>

      {error !== null ? (
        <p className="border-b border-stone-200 px-5 py-4 text-sm text-red-700 lg:px-6" role="alert">
          {error}
        </p>
      ) : null}

      {loading ? (
        <p className="px-5 py-8 text-sm text-stone-500 lg:px-6">Loading keys…</p>
      ) : keys.length === 0 ? (
        <div className="px-5 py-12 text-center lg:px-6">
          <KeyRound className="mx-auto text-stone-300" aria-hidden="true" size={28} />
          <h3 className="mt-4 font-semibold text-stone-950">No API keys yet</h3>
          <p className="mx-auto mt-2 max-w-md text-sm leading-6 text-stone-500">
            Create one to let an MCP client such as Claude Code or Codex search your
            workspaces and query CSV files on your behalf.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-stone-200">
          {activeKeys.map((key) => (
            <div key={key.id} className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center sm:justify-between lg:px-6">
              <div className="min-w-0">
                <p className="truncate font-medium text-stone-950">{key.name}</p>
                <p className="mt-1 font-mono text-xs text-stone-500">
                  {key.key_prefix}… · created {formatDate(key.created_at)} ·{' '}
                  {key.last_used_at === null ? 'never used' : `last used ${formatDate(key.last_used_at)}`}
                </p>
              </div>
              <button
                type="button"
                className="text-button shrink-0 text-red-700 hover:text-red-900"
                onClick={() => setRevokeTarget(key)}
              >
                <Trash2 aria-hidden="true" size={15} />
                Revoke
              </button>
            </div>
          ))}
          {revokedKeys.length > 0 ? (
            <div className="px-5 py-3 lg:px-6">
              <p className="font-mono text-[0.65rem] uppercase tracking-[0.14em] text-stone-400">
                {revokedKeys.length} revoked key{revokedKeys.length === 1 ? '' : 's'} retained for audit
              </p>
            </div>
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
          <div className="px-5 py-6 sm:px-7">
            <label className="field-label" htmlFor="api-key-name">
              Key name
            </label>
            <input
              id="api-key-name"
              className="field-input mt-2"
              value={newName}
              maxLength={100}
              placeholder="laptop · Claude Code · ci-agent"
              autoFocus
              onChange={(event) => setNewName(event.target.value)}
            />
          </div>
          <div className="flex flex-col-reverse gap-3 border-t border-stone-200 px-5 py-5 sm:flex-row sm:justify-end sm:px-7">
            <button type="button" className="button-secondary" disabled={creating} onClick={() => setCreateOpen(false)}>
              Cancel
            </button>
            <button type="submit" className="button-primary" disabled={creating || newName.trim() === ''}>
              {creating ? 'Creating…' : 'Create key'}
            </button>
          </div>
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
          <div className="px-5 py-6 sm:px-7">
            <div className="mb-5 flex items-start gap-3 border border-amber-300 bg-amber-50 p-4 text-sm leading-6 text-amber-950">
              <KeyRound className="mt-0.5 shrink-0" aria-hidden="true" size={18} />
              <p>{revealed.warning}</p>
            </div>
            <div className="border-y border-stone-200 bg-stone-50 px-4 py-3">
              <p className="text-sm text-stone-500">Key</p>
              <p className="mt-1 break-all font-mono text-sm font-semibold text-stone-950">{revealed.key}</p>
            </div>
            <p className="mt-4 text-sm leading-6 text-stone-600">
              Send it as <code className="font-mono text-[0.85em]">Authorization: Bearer &lt;key&gt;</code> when
              connecting an MCP client to <code className="font-mono text-[0.85em]">/mcp</code>.
            </p>
            <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
              <button type="button" className="button-secondary" onClick={() => setRevealed(null)}>
                I have stored it safely
              </button>
              <button type="button" className="button-primary" onClick={copyKey}>
                {copied ? <Check aria-hidden="true" size={17} /> : <Copy aria-hidden="true" size={17} />}
                {copied ? 'Copied' : 'Copy key'}
              </button>
            </div>
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
    </section>
  )
}

function formatDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}
