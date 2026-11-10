import { lazy, Suspense, useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import type { ToolStep } from '../hooks/useAgentConversation'
import type { AgentChatSource } from '../types'

const MarkdownText = lazy(() => import('./MarkdownText').then((m) => ({ default: m.MarkdownText })))

interface ChatAnswerCardProps {
  calls: ToolStep[]
  texts: string[]
  sources?: AgentChatSource[]
  streaming: boolean
  busy: boolean
  errors: string[]
}

interface SourceRef {
  index: number
  filename: string
  ordinal?: number
  page?: number
  documentId?: string
}

const SOURCE_LINE = /^\[(\d+)\]\s+filename=("(?:\\.|[^"\\])*")(.*)$/gm

function parseSources(calls: ToolStep[]): SourceRef[] {
  const seen = new Set<string>()
  const sources: SourceRef[] = []
  for (const call of calls) {
    for (const match of call.preview.matchAll(SOURCE_LINE)) {
      const index = Number.parseInt(match[1], 10)
      let filename = match[2]
      try {
        filename = JSON.parse(filename) as string
      } catch {
        filename = filename.replace(/^"|"$/g, '')
      }
      const rest = match[3]
      const ordinalMatch = /\bordinal=(\d+)/.exec(rest)
      const pageMatch = /\bpages=(\d+)/.exec(rest)
      const documentMatch = /\bdocument_id=([0-9a-fA-F-]{36})/.exec(rest)
      const source: SourceRef = {
        index,
        filename,
        ordinal: ordinalMatch === null ? undefined : Number.parseInt(ordinalMatch[1], 10),
        page: pageMatch === null ? undefined : Number.parseInt(pageMatch[1], 10),
        documentId: documentMatch?.[1],
      }
      const key = `${source.documentId ?? source.filename}:${source.ordinal ?? source.index}`
      if (seen.has(key)) continue
      seen.add(key)
      sources.push(source)
    }
  }
  return sources.sort((left, right) => left.index - right.index)
}

function serverSources(sources: AgentChatSource[]): SourceRef[] {
  return sources
    .map((source) => ({
      index: source.index,
      filename: source.filename ?? 'unknown',
      ordinal: source.ordinal,
      page: source.page_numbers?.[0],
      documentId: source.document_id,
    }))
    .sort((left, right) => left.index - right.index)
}

function documentRef(source: SourceRef | undefined): string | null {
  if (source?.documentId === undefined) return null
  return `DOC-${source.documentId.replaceAll('-', '').slice(-4).toUpperCase()}`
}

function citationLocation(source: SourceRef): string {
  const extension = source.filename.toLowerCase().split('.').pop() ?? ''
  if (['csv', 'xlsx', 'json'].includes(extension) && source.ordinal !== undefined) {
    return `r.${source.ordinal}`
  }
  if (source.page !== undefined) return `p.${source.page}`
  if (source.ordinal !== undefined) return `source ${source.index}`
  return `source ${source.index}`
}

function argumentSummary(call: ToolStep): string {
  const preferred = ['query', 'pattern', 'sql', 'document_id', 'ref', 'start_ordinal']
  const found = preferred
    .map((key) => [key, call.arguments[key]] as const)
    .find(([, value]) => value !== undefined && value !== null)
  if (found === undefined) return ''
  const [key, value] = found
  if (key === 'document_id' && typeof value === 'string') {
    return `DOC-${value.replaceAll('-', '').slice(-4).toUpperCase()}`
  }
  const text = String(value).replace(/\s+/g, ' ').trim()
  return text.length > 84 ? `${text.slice(0, 81)}…` : text
}

function formatDuration(call: ToolStep, running: boolean): string {
  if (running) return 'running'
  if (call.durationMs === undefined) return ''
  return `${(call.durationMs / 1000).toFixed(1)}s`
}

/**
 * One answer, framed the way the evidence is framed in the product:
 * an answer plate with its source reference, the prose, the calls that
 * produced it, and the citations that make it checkable.
 */
export function ChatAnswerCard({
  calls,
  texts,
  sources: citedSources,
  streaming,
  busy,
  errors,
}: ChatAnswerCardProps) {
  const [trailOpen, setTrailOpen] = useState(false)
  const citationPrefix = `citation-${useId().replaceAll(':', '')}`
  const answer = texts.join('\n\n')
  const hasAnswer = answer.trim() !== ''
  const sources =
    citedSources === undefined ? parseSources(calls) : serverSources(citedSources)
  const firstSource = sources[0]
  const reference = documentRef(firstSource)
  const location =
    sources.length > 1
      ? `${sources.length} sources`
      : firstSource?.page !== undefined
        ? `page ${firstSource.page}`
        : firstSource?.ordinal !== undefined
          ? `source ${firstSource.index}`
          : null
  const stamp = hasAnswer ? 'Answer' : busy ? 'Working' : 'Answer'
  const lastCall = calls[calls.length - 1]
  const running = busy && lastCall !== undefined && lastCall.preview === ''
  const totalMs = calls.reduce((total, call) => total + (call.durationMs ?? 0), 0)
  const totalDuration = totalMs > 0 ? `${(totalMs / 1000).toFixed(1)}s` : ''

  return (
    <article className="chat-answer-card" aria-label="Answer">
      <header className="chat-answer-header">
        <span className="chat-answer-stamp">{stamp}</span>
        {reference === null ? null : <span className="chat-doc-chip">{reference}</span>}
        {location === null ? null : (
          <span className="chat-answer-location">{location}</span>
        )}
      </header>

      <div className="chat-answer-body">
        {hasAnswer ? (
          streaming ? (
            <p className="chat-answer-stream">
              {answer}
              <span className="chat-stream-cursor" aria-hidden="true" />
            </p>
          ) : (
            <div className="chat-answer-md">
              <Suspense fallback={<p className="whitespace-pre-wrap">{answer}</p>}>
                <MarkdownText
                  text={answer}
                  citations={sources.map((source) => source.index)}
                  citationPrefix={citationPrefix}
                />
              </Suspense>
            </div>
          )
        ) : (
          <p className="chat-working" role="status">
            <span className="chat-working-dot" aria-hidden="true" />
            {busy ? 'Reading the workspace…' : 'No answer was returned.'}
          </p>
        )}

        {errors.map((error, index) => (
          <p className="chat-error" key={index} role="alert">
            {error}
          </p>
        ))}
      </div>

      {calls.length === 0 ? null : (
        <div className="chat-tool-trail" aria-label="Tool activity">
          <button
            type="button"
            className="chat-tool-summary"
            aria-expanded={trailOpen}
            onClick={() => setTrailOpen((open) => !open)}
          >
            <span
              className={`chat-tool-dot ${running ? 'chat-tool-dot-running' : ''}`}
              aria-hidden="true"
            />
            <span className="chat-tool-name">
              {running
                ? lastCall.name
                : `${calls.length} tool call${calls.length === 1 ? '' : 's'}`}
            </span>
            {running ? <span className="chat-tool-arg">running</span> : null}
            {running || totalDuration === '' ? null : (
              <span className="chat-tool-time">{totalDuration}</span>
            )}
            <ChevronDown
              aria-hidden="true"
              size={12}
              className={`chat-tool-chevron${trailOpen ? ' chat-tool-chevron-open' : ''}`}
            />
          </button>

          {trailOpen ? (
            <div className="chat-tool-list">
              {calls.map((call, index) => {
                const callRunning = running && index === calls.length - 1
                const summary = argumentSummary(call)
                const duration = formatDuration(call, callRunning)
                return (
                  <div
                    className="chat-tool-row"
                    key={`${call.name}-${index}`}
                    title={JSON.stringify(call.arguments)}
                  >
                    <span
                      className={`chat-tool-dot ${callRunning ? 'chat-tool-dot-running' : ''}`}
                      aria-hidden="true"
                    />
                    <span className="chat-tool-name">{call.name}</span>
                    {summary === '' ? null : <span className="chat-tool-arg">“{summary}”</span>}
                    {duration === '' ? null : <span className="chat-tool-time">{duration}</span>}
                  </div>
                )
              })}
            </div>
          ) : null}
        </div>
      )}

      {sources.length === 0 ? null : (
        <div className="chat-citations" aria-label="Sources">
          {sources.map((source) => (
            <span
              className="chat-citation"
              id={`${citationPrefix}-${source.index}`}
              key={`${source.documentId ?? source.filename}-${source.ordinal ?? source.index}`}
            >
              <span className="chat-citation-index">[{source.index}]</span>
              {source.filename} · {citationLocation(source)}
            </span>
          ))}
        </div>
      )}
    </article>
  )
}
