import { useEffect, useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import { agentAdmin, getErrorMessage, isUnauthorized } from '../api'
import type { AgentModelSettings, AgentProviderInfo } from '../types'

/**
 * The one chat model every agent session uses, configured here.
 *
 * A built-in provider owns its base URL and its default model, so the admin
 * picks the provider and pastes a key; only the custom entry asks for an
 * address. Saving always keeps the settings and reports the live connection
 * test: an endpoint that answers with a different model than the one named,
 * or not at all, is shown here instead of surfacing the first time someone
 * asks the agent a question.
 */
export function AdminAgentModel({
  onUnauthorized,
  onNotice,
}: {
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
}) {
  const [settings, setSettings] = useState<AgentModelSettings | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [provider, setProvider] = useState('custom')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    agentAdmin
      .get(controller.signal)
      .then((loaded) => {
        setSettings(loaded)
        setProvider(loaded.provider ?? 'custom')
        setBaseUrl(loaded.base_url ?? '')
        setModel(loaded.model ?? '')
      })
      .catch((caught) => {
        // An aborted fetch is this component unmounting or re-running its
        // effect, not a failure: reporting it would render an empty error
        // in place of the form the retry then fills in.
        if (controller.signal.aborted) return
        if (isUnauthorized(caught)) onUnauthorized()
        else {
          setLoadError(getErrorMessage(caught) || 'The configuration could not be loaded.')
        }
      })
    return () => controller.abort()
  }, [onUnauthorized])

  const providers: AgentProviderInfo[] = settings?.available_providers ?? []
  const chosen = providers.find((entry) => entry.key === provider)
  const isCustom = provider === 'custom'

  const pickProvider = (key: string) => {
    setProvider(key)
    const preset = providers.find((entry) => entry.key === key)
    if (preset !== undefined && preset.default_model !== '') setModel(preset.default_model)
  }

  const save = async () => {
    if (saving) return
    setSaving(true)
    try {
      const saved = await agentAdmin.set({
        provider,
        ...(isCustom ? { base_url: baseUrl.trim() } : {}),
        ...(model.trim() === '' ? {} : { model: model.trim() }),
        ...(apiKey.trim() === '' ? {} : { api_key: apiKey.trim() }),
      })
      setSettings(saved)
      setApiKey('')
      if (saved.last_test_ok) {
        onNotice(`Agent model saved; the endpoint answered as ${saved.last_test_model}.`)
      } else {
        onNotice(
          `Agent model saved, but the connection test failed: ${saved.test_error}`,
          'error',
        )
      }
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setSaving(false)
    }
  }

  if (loadError !== null) {
    return (
      <section className="card p-5">
        <p className="eyebrow">Agent harness</p>
        <p className="mt-2 text-sm text-ink-faint">{loadError}</p>
      </section>
    )
  }
  if (settings === null) {
    return (
      <section className="card flex items-center gap-2 p-5 text-sm text-ink-faint">
        <LoaderCircle aria-hidden="true" size={15} className="animate-spin" />
        Loading the agent model configuration…
      </section>
    )
  }

  return (
    <section className="card p-5">
      <p className="eyebrow">Agent harness</p>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">Chat model</h2>
        <span className="stamp">
          {settings.configured ? `configured · ${settings.model}` : 'not configured'}
        </span>
        {settings.configured && settings.last_test_ok === false ? (
          <span className="stamp border-danger/40 text-danger">connection test failed</span>
        ) : null}
      </div>
      <p className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-faint">
        One OpenAI-compatible endpoint answers every workspace agent. Pick a
        provider, paste its API key
        {settings.api_key_preview !== null ? ` (currently ${settings.api_key_preview})` : ''}
        , and the server fills in the rest. The key is stored server-side and
        never shown again; saving runs a live connection test and reports the
        model that actually answered.
      </p>

      <form
        className="mt-4 grid gap-4 sm:grid-cols-2"
        onSubmit={(event) => {
          event.preventDefault()
          void save()
        }}
      >
        <label className="block">
          <span className="label">Provider</span>
          <select
            className="field"
            value={provider}
            onChange={(event) => pickProvider(event.target.value)}
          >
            {providers.map((entry) => (
              <option key={entry.key} value={entry.key}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="label">Model</span>
          <input
            className="field"
            value={model}
            required
            maxLength={200}
            placeholder={chosen?.default_model || 'the model id the gateway routes to'}
            onChange={(event) => setModel(event.target.value)}
          />
        </label>
        {isCustom ? (
          <label className="block sm:col-span-2">
            <span className="label">Base URL</span>
            <input
              className="field"
              value={baseUrl}
              required
              maxLength={500}
              placeholder="https://gateway.example/v1"
              onChange={(event) => setBaseUrl(event.target.value)}
            />
          </label>
        ) : null}
        <label className="block sm:col-span-2">
          <span className="label">API key</span>
          <input
            className="field"
            type="password"
            value={apiKey}
            maxLength={500}
            autoComplete="off"
            placeholder={
              settings.configured ? `leave blank to keep ${settings.api_key_preview}` : 'required'
            }
            required={!settings.configured}
            onChange={(event) => setApiKey(event.target.value)}
          />
          {chosen !== undefined && chosen.key_hint !== '' ? (
            <span className="mt-1 block text-xs text-ink-faint">{chosen.key_hint}</span>
          ) : null}
        </label>
        <div className="flex items-center gap-3 sm:col-span-2">
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? <LoaderCircle aria-hidden="true" size={14} className="animate-spin" /> : null}
            Save and test
          </button>
          {settings.last_test_model !== null ? (
            <span className="text-xs text-ink-faint">
              Last test answered as {settings.last_test_model}
            </span>
          ) : null}
        </div>
      </form>
    </section>
  )
}
