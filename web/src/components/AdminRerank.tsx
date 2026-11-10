import { useEffect, useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import { getErrorMessage, isUnauthorized, retrievalAdmin } from '../api'
import type { RetrievalSettings, RetrievalUpdate } from '../types'

/**
 * The optional Laya reranker, configured beside the chat model.
 *
 * Off by default: the retrieval path stays cosine-only until an admin turns
 * this on, and any failure at the decision endpoint falls back to that same
 * cosine order. The endpoint and key are the embedding server's, because one
 * LM Studio instance already serves both Qwen and Laya.
 */
export function AdminRerank({
  onUnauthorized,
  onNotice,
}: {
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
}) {
  const [settings, setSettings] = useState<RetrievalSettings | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [enabled, setEnabled] = useState(false)
  const [model, setModel] = useState('laya')
  const [candidates, setCandidates] = useState('30')
  const [concurrency, setConcurrency] = useState('8')
  const [minScore, setMinScore] = useState('')
  const [timeoutMs, setTimeoutMs] = useState('10000')
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)

  const apply = (loaded: RetrievalSettings) => {
    setSettings(loaded)
    setEnabled(loaded.rerank_enabled)
    setModel(loaded.rerank_model)
    setCandidates(String(loaded.rerank_candidates))
    setConcurrency(String(loaded.rerank_concurrency))
    setMinScore(loaded.rerank_min_score === null ? '' : String(loaded.rerank_min_score))
    setTimeoutMs(String(loaded.rerank_timeout_ms))
  }

  useEffect(() => {
    const controller = new AbortController()
    retrievalAdmin
      .get(controller.signal)
      .then(apply)
      .catch((caught) => {
        if (controller.signal.aborted) return
        if (isUnauthorized(caught)) onUnauthorized()
        else setLoadError(getErrorMessage(caught) || 'The retrieval settings could not be loaded.')
      })
    return () => controller.abort()
  }, [onUnauthorized])

  const update = (): RetrievalUpdate => ({
    rerank_enabled: enabled,
    rerank_model: model.trim() || 'laya',
    rerank_candidates: Number.parseInt(candidates, 10) || 30,
    rerank_concurrency: Number.parseInt(concurrency, 10) || 8,
    rerank_min_score:
      minScore.trim() === '' ? null : Number.parseFloat(minScore),
    rerank_timeout_ms: Number.parseInt(timeoutMs, 10) || 3000,
  })

  const save = async () => {
    if (saving) return
    setSaving(true)
    try {
      const saved = await retrievalAdmin.set(update())
      apply(saved)
      if (saved.last_test_ok) {
        onNotice(
          `Retrieval settings saved; Laya answered in ${saved.last_test_ms ?? '?'} ms.`,
        )
      } else {
        onNotice(
          `Retrieval settings saved, but the Laya test failed: ${saved.last_test_error ?? 'no response'}`,
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

  const test = async () => {
    if (testing) return
    setTesting(true)
    try {
      const result = await retrievalAdmin.test(update())
      if (result.ok) {
        onNotice(
          `Laya answered in ${result.latency_ms ?? '?'} ms with score ${result.score?.toFixed(2) ?? '?'}.`,
        )
      } else {
        onNotice(`Laya test failed: ${result.error ?? 'no response'}`, 'error')
      }
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setTesting(false)
    }
  }

  if (loadError !== null) {
    return (
      <section className="panel p-5">
        <p className="eyebrow">Retrieval</p>
        <p className="mt-2 text-sm text-ink-faint">{loadError}</p>
      </section>
    )
  }
  if (settings === null) {
    return (
      <section className="panel flex items-center gap-2 p-5 text-sm text-ink-faint">
        <LoaderCircle aria-hidden="true" size={15} className="animate-spin" />
        Loading the retrieval settings…
      </section>
    )
  }

  return (
    <section className="panel p-5">
      <p className="eyebrow">Retrieval</p>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <h2 className="font-condensed text-sm font-bold uppercase tracking-[0.06em] text-ink">
          Reranking
        </h2>
        <span className="stamp">{enabled ? `on · ${model}` : 'off'}</span>
        {settings.last_test_ok === false ? (
          <span className="stamp border-danger/40 text-danger">last test failed</span>
        ) : null}
      </div>
      <p className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-faint">
        A second retrieval stage scores the top candidates with Laya before the
        agent sees them. It uses the same local endpoint as the embeddings
        (<span className="font-mono text-[0.72rem]">{settings.embedding_base_url}</span>,
        embedding model <span className="font-mono text-[0.72rem]">{settings.embedding_model}</span>).
        When it is off, or when a request fails, the cosine order is used unchanged.
      </p>

      <form
        className="mt-4 grid gap-4 sm:grid-cols-3"
        onSubmit={(event) => {
          event.preventDefault()
          void save()
        }}
      >
        <label className="flex items-center gap-2 sm:col-span-3">
          <input
            type="checkbox"
            checked={enabled}
            style={{ accentColor: 'var(--color-accent)' }}
            onChange={(event) => setEnabled(event.target.checked)}
          />
          <span className="text-sm font-medium text-ink">Enable Laya reranking</span>
          <span className="text-xs text-ink-faint">default off; cosine order is the fallback</span>
        </label>

        <label className="block">
          <span className="label">Model</span>
          <input
            className="field"
            value={model}
            required
            maxLength={200}
            placeholder="laya"
            onChange={(event) => setModel(event.target.value)}
          />
        </label>
        <label className="block">
          <span className="label">Candidates</span>
          <input
            className="field"
            type="number"
            min={1}
            max={100}
            value={candidates}
            onChange={(event) => setCandidates(event.target.value)}
          />
        </label>
        <label className="block">
          <span className="label">Concurrency</span>
          <input
            className="field"
            type="number"
            min={1}
            max={16}
            value={concurrency}
            onChange={(event) => setConcurrency(event.target.value)}
          />
        </label>
        <label className="block">
          <span className="label">Minimum score</span>
          <input
            className="field"
            type="number"
            min={0}
            max={2}
            step="0.1"
            value={minScore}
            placeholder="keep all"
            onChange={(event) => setMinScore(event.target.value)}
          />
        </label>
        <label className="block">
          <span className="label">Timeout (ms)</span>
          <input
            className="field"
            type="number"
            min={100}
            max={60000}
            value={timeoutMs}
            onChange={(event) => setTimeoutMs(event.target.value)}
          />
        </label>
        <div className="flex flex-wrap items-center gap-3 sm:col-span-3">
          <button type="submit" className="btn btn-primary" disabled={saving || testing}>
            {saving ? <LoaderCircle aria-hidden="true" size={14} className="animate-spin" /> : null}
            Save and test
          </button>
          <button
            type="button"
            className="btn"
            disabled={saving || testing}
            onClick={() => void test()}
          >
            {testing ? <LoaderCircle aria-hidden="true" size={14} className="animate-spin" /> : null}
            Test only
          </button>
          {settings.last_tested_at !== null ? (
            <span className="text-xs text-ink-faint">
              Last test {settings.last_test_ok ? 'passed' : 'failed'}
              {settings.last_test_ms !== null ? ` in ${settings.last_test_ms} ms` : ''}
            </span>
          ) : null}
        </div>
      </form>
    </section>
  )
}
