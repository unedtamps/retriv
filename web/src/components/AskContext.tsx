import { useEffect, useRef, useState, type FormEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  BookOpenText,
  CircleAlert,
  CircleHelp,
  Download,
  LoaderCircle,
  MessageSquareText,
  Plus,
  Send,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { AgentConversation, AgentConversationTurn, Workspace } from '../types'

const MARKDOWN_PLUGINS = [remarkGfm]
const MARKDOWN_COMPONENTS = { img: () => null }

interface AskContextProps {
  token: string
  workspaces: Workspace[]
  onUnauthorized: () => void
}

export function AskContext({ token, workspaces, onUnauthorized }: AskContextProps) {
  const eligibleWorkspaces = workspaces.filter((workspace) => workspace.can_search_context)
  const [conversations, setConversations] = useState<AgentConversation[]>([])
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null)
  const [turns, setTurns] = useState<AgentConversationTurn[]>([])
  const [question, setQuestion] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loadingConversations, setLoadingConversations] = useState(true)
  const [turnsLoadedForConversationId, setTurnsLoadedForConversationId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [creating, setCreating] = useState(false)
  const [downloadingDocumentKey, setDownloadingDocumentKey] = useState<string | null>(null)
  const loadingTurns = selectedConversationId !== null
    && turnsLoadedForConversationId !== selectedConversationId
  const conversationDocuments = loadingTurns
    ? []
    : downloadableConversationDocuments(turns, workspaces)
  const requestController = useRef<AbortController | null>(null)
  const turnsLoadSequence = useRef(0)
  const selectedConversationRef = useRef(selectedConversationId)
  const onUnauthorizedRef = useRef(onUnauthorized)

  useEffect(() => () => requestController.current?.abort(), [])
  useEffect(() => {
    selectedConversationRef.current = selectedConversationId
  }, [selectedConversationId])
  useEffect(() => {
    onUnauthorizedRef.current = onUnauthorized
  }, [onUnauthorized])

  useEffect(() => {
    const controller = new AbortController()
    api.listAgentConversations(token, { signal: controller.signal })
      .then((page) => {
        setConversations(page.items)
        setSelectedConversationId((current) => {
          if (current !== null) return current
          const next = page.items[0]?.id ?? null
          selectedConversationRef.current = next
          return next
        })
        setError(null)
      })
      .catch((caught) => {
        if (caught instanceof Error && caught.name === 'AbortError') return
        if (isUnauthorized(caught)) onUnauthorizedRef.current()
        else setError(getErrorMessage(caught))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingConversations(false)
      })
    return () => controller.abort()
  }, [token])

  useEffect(() => {
    if (selectedConversationId === null) {
      return
    }
    const controller = new AbortController()
    const loadSequence = turnsLoadSequence.current + 1
    turnsLoadSequence.current = loadSequence
    api.listAgentConversationTurns(token, selectedConversationId, {
      limit: 100,
      signal: controller.signal,
    })
      .then((page) => {
        if (turnsLoadSequence.current === loadSequence) {
          setTurns(page.items)
          setTurnsLoadedForConversationId(selectedConversationId)
          setError(null)
        }
      })
      .catch((caught) => {
        if (caught instanceof Error && caught.name === 'AbortError') return
        setTurnsLoadedForConversationId(selectedConversationId)
        if (isUnauthorized(caught)) onUnauthorizedRef.current()
        else setError(getErrorMessage(caught))
      })
    return () => controller.abort()
  }, [token, selectedConversationId])

  async function refreshConversations(): Promise<AgentConversation[]> {
    const page = await api.listAgentConversations(token)
    setConversations(page.items)
    return page.items
  }

  async function createConversation() {
    if (creating) return
    setCreating(true)
    setError(null)
    try {
      const conversation = await api.createAgentConversation(token)
      setConversations((current) => [
        conversation,
        ...current.filter((item) => item.id !== conversation.id),
      ])
      setTurns([])
      setTurnsLoadedForConversationId(conversation.id)
      selectedConversationRef.current = conversation.id
      setSelectedConversationId(conversation.id)
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorizedRef.current()
      else setError(getErrorMessage(caught))
    } finally {
      setCreating(false)
    }
  }

  async function deleteConversation(conversation: AgentConversation) {
    if (!window.confirm(`Delete “${conversation.title ?? 'New conversation'}”?`)) return
    setError(null)
    try {
      await api.deleteAgentConversation(token, conversation.id)
      const remaining = await refreshConversations()
      if (selectedConversationId === conversation.id) {
        setTurns([])
        const next = remaining[0]?.id ?? null
        setTurnsLoadedForConversationId(null)
        selectedConversationRef.current = next
        setSelectedConversationId(next)
      }
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorizedRef.current()
      else setError(getErrorMessage(caught))
    }
  }

  async function downloadConversationDocument(sourceDocument: ConversationDocument) {
    setDownloadingDocumentKey(sourceDocument.key)
    setError(null)
    try {
      const blob = await api.downloadDocument(
        token,
        sourceDocument.workspace_id,
        sourceDocument.document_id,
      )
      const objectUrl = window.URL.createObjectURL(blob)
      const link = window.document.createElement('a')
      link.href = objectUrl
      link.download = sourceDocument.filename ?? `document-${sourceDocument.document_id}`
      link.style.display = 'none'
      window.document.body.append(link)
      link.click()
      link.remove()
      window.setTimeout(() => window.URL.revokeObjectURL(objectUrl), 1000)
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorizedRef.current()
      else setError(getErrorMessage(caught))
    } finally {
      setDownloadingDocumentKey(null)
    }
  }

  async function sendTurn(
    conversationId: string,
    turnQuestion: string,
    requestId: string,
  ) {
    setSubmitting(true)
    setError(null)
    const controller = new AbortController()
    requestController.current?.abort()
    requestController.current = controller
    try {
      const turn = await api.sendAgentConversationTurn(
        token,
        conversationId,
        requestId,
        turnQuestion,
        controller.signal,
      )
      turnsLoadSequence.current += 1
      if (selectedConversationRef.current === conversationId) {
        setTurnsLoadedForConversationId(conversationId)
        setTurns((current) => {
          const next = current.filter((item) => item.id !== turn.id)
          next.push(turn)
          return next.sort((left, right) => left.sequence - right.sequence)
        })
      }
      try {
        await refreshConversations()
      } catch (caught) {
        if (isUnauthorized(caught)) onUnauthorizedRef.current()
        else setError(getErrorMessage(caught))
      }
    } catch (caught) {
      if (caught instanceof Error && caught.name === 'AbortError') return
      if (isUnauthorized(caught)) onUnauthorizedRef.current()
      else setError(getErrorMessage(caught))
      try {
        const page = await api.listAgentConversationTurns(token, conversationId, { limit: 100 })
        if (selectedConversationRef.current === conversationId) {
          turnsLoadSequence.current += 1
          setTurns(page.items)
          setTurnsLoadedForConversationId(conversationId)
        }
      } catch {
        // Keep the original send error visible if refreshing the transcript also fails.
      }
    } finally {
      if (requestController.current === controller) requestController.current = null
      setSubmitting(false)
    }
  }

  async function submitQuestion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const normalized = question.trim()
    if (
      normalized.length === 0
      || submitting
      || loadingTurns
      || selectedConversationId === null
    ) return
    setQuestion('')
    await sendTurn(selectedConversationId, normalized, createRequestId())
  }

  return (
    <div className="mx-auto flex h-[calc(100dvh-4rem)] w-full flex-col overflow-hidden px-3 py-3 sm:px-5 lg:px-6 lg:py-4">
      {error === null ? null : (
        <p className="form-error mb-3 flex shrink-0 items-start gap-2" role="alert">
          <CircleAlert aria-hidden="true" className="mt-0.5 shrink-0" size={17} />
          {error}
        </p>
      )}

      <div className="grid min-h-0 flex-1 grid-rows-[auto_minmax(0,1fr)] gap-3 lg:grid-cols-[17rem_minmax(0,1fr)] lg:grid-rows-1 lg:gap-4">
        <aside className="flex max-h-40 min-h-0 flex-col overflow-hidden border border-stone-300 bg-white lg:max-h-none" aria-label="Your conversations">
          <div className="flex items-center justify-between border-b border-stone-200 px-4 py-4">
            <h2 className="font-semibold text-stone-950">Conversations</h2>
            <button
              type="button"
              className="icon-button"
              aria-label="Start a new conversation"
              disabled={creating || submitting || loadingConversations}
              onClick={createConversation}
            >
              {creating ? <LoaderCircle className="animate-spin" size={17} /> : <Plus size={17} />}
            </button>
          </div>
          {loadingConversations ? (
            <div className="flex flex-1 items-center justify-center text-stone-500" role="status">
              <LoaderCircle className="animate-spin" size={20} />
            </div>
          ) : conversations.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <MessageSquareText className="mx-auto text-stone-400" size={24} aria-hidden="true" />
              <p className="mt-3 text-sm font-medium text-stone-800">No conversations yet</p>
              <button type="button" className="text-button mt-3" disabled={creating || submitting || loadingConversations} onClick={createConversation}>
                Start a conversation <Plus size={14} />
              </button>
            </div>
          ) : (
            <ul className="min-h-0 flex-1 divide-y divide-stone-100 overflow-y-auto overscroll-contain">
              {conversations.map((conversation) => (
                <li key={conversation.id} className="group relative">
                  <button
                    type="button"
                    className={conversation.id === selectedConversationId
                      ? 'w-full bg-emerald-50 px-4 py-3 pr-12 text-left'
                      : 'w-full px-4 py-3 pr-12 text-left hover:bg-stone-50'}
                    aria-current={conversation.id === selectedConversationId ? 'true' : undefined}
                    disabled={submitting}
                    onClick={() => {
                      selectedConversationRef.current = conversation.id
                      setSelectedConversationId(conversation.id)
                    }}
                  >
                    <span className="block truncate text-sm font-medium text-stone-900">
                      {conversation.title ?? 'New conversation'}
                    </span>
                    <span className="mt-1 block font-mono text-[0.62rem] text-stone-500">
                      {new Date(conversation.updated_at).toLocaleDateString()}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="icon-button absolute right-2 top-2 opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
                    aria-label={`Delete ${conversation.title ?? 'conversation'}`}
                    disabled={submitting}
                    onClick={() => void deleteConversation(conversation)}
                  >
                    <Trash2 size={15} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <section className="flex min-h-0 flex-col overflow-hidden border border-stone-300 bg-white" aria-label="Conversation">
          {selectedConversationId === null ? (
            <div className="flex flex-1 flex-col items-center justify-center px-6 py-12 text-center">
              <MessageSquareText className="text-emerald-800" size={30} aria-hidden="true" />
              <h2 className="mt-4 text-lg font-semibold text-stone-950">Start a private conversation</h2>
              <p className="mt-2 max-w-md text-sm leading-6 text-stone-600">
                Your conversation history is private to your account. Raw retrieved chunks and tool outputs are not stored separately; saved answers may quote source material.
              </p>
              <button type="button" className="button-primary mt-5" onClick={createConversation} disabled={creating || submitting || loadingConversations}>
                <Plus size={16} aria-hidden="true" />New conversation
              </button>
            </div>
          ) : (
            <>
              <div className="flex shrink-0 items-center gap-3 border-b border-stone-200 bg-white px-5 py-4 sm:px-7">
                <MessageSquareText className="text-emerald-800" size={19} aria-hidden="true" />
                <div className="min-w-0">
                  <p className="truncate text-sm font-semibold text-stone-950">
                    {conversations.find((item) => item.id === selectedConversationId)?.title ?? 'New conversation'}
                  </p>
                  <p className="text-xs text-stone-500">Private to your account · document evidence is searched again for each turn</p>
                </div>
              </div>

              {conversationDocuments.length === 0 ? null : (
                <details className="shrink-0 border-b border-stone-200 bg-white px-5 py-3 sm:px-7">
                  <summary className="flex cursor-pointer items-center gap-2 text-sm font-medium text-emerald-900">
                    <Download size={16} aria-hidden="true" />
                    Documents in this conversation ({conversationDocuments.length})
                  </summary>
                  <ul className="mt-3 max-h-40 space-y-2 overflow-y-auto overscroll-contain pr-2">
                    {conversationDocuments.map((sourceDocument) => {
                      const filename = sourceDocument.filename ?? 'Workspace document'
                      const isDownloading = downloadingDocumentKey === sourceDocument.key
                      return (
                        <li
                          key={sourceDocument.key}
                          className="flex flex-wrap items-center justify-between gap-3 border-l-2 border-stone-300 pl-3 text-sm"
                        >
                          <span className="min-w-0">
                            <span className="block truncate font-medium text-stone-800">{filename}</span>
                            <span className="text-xs text-stone-500">{sourceDocument.workspace_name}</span>
                          </span>
                          <button
                            type="button"
                            className="text-button shrink-0"
                            aria-label={`Download ${filename}`}
                            disabled={downloadingDocumentKey !== null}
                            onClick={() => void downloadConversationDocument(sourceDocument)}
                          >
                            {isDownloading
                              ? <LoaderCircle className="animate-spin" size={14} />
                              : <Download size={14} aria-hidden="true" />}
                            {isDownloading ? 'Downloading…' : 'Download'}
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                </details>
              )}

              <div
                role="region"
                aria-label="Conversation transcript"
                className="min-h-0 flex-1 space-y-5 overflow-y-auto overscroll-contain px-4 py-5 sm:px-7"
              >
                {loadingTurns ? (
                  <div className="flex items-center gap-2 text-sm text-stone-500" role="status">
                    <LoaderCircle className="animate-spin" size={17} />Loading conversation…
                  </div>
                ) : turns.length === 0 ? (
                  <div className="py-12 text-center">
                    <CircleHelp className="mx-auto text-stone-400" size={26} aria-hidden="true" />
                    <p className="mt-3 text-sm font-medium text-stone-800">Ask a question to begin</p>
                    <p className="mt-1 text-xs text-stone-500">Each turn uses your current workspace access.</p>
                  </div>
                ) : turns.map((turn) => (
                  <article key={turn.id} className="space-y-3">
                    <div className="ml-auto max-w-[90%] border border-stone-200 bg-stone-50 p-4 sm:max-w-[80%]">
                      <p className="section-eyebrow">You</p>
                      <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-stone-800">{turn.question}</p>
                    </div>
                    {turn.status === 'processing' ? (
                      <div className="flex items-center gap-2 text-sm text-stone-500" role="status">
                        <LoaderCircle className="animate-spin" size={17} />Thinking…
                      </div>
                    ) : turn.status === 'failed' ? (
                      <div className="border border-red-200 bg-red-50 p-4">
                        <p className="text-sm text-red-900">The answer could not be completed.</p>
                        <button
                          type="button"
                          className="text-button mt-2"
                          disabled={submitting}
                          onClick={() => void sendTurn(selectedConversationId, turn.question, turn.request_id)}
                        >
                          Retry <Send size={14} />
                        </button>
                      </div>
                    ) : (
                      <div className="max-w-[95%] p-4 sm:max-w-[88%]">
                        <h3 className="section-eyebrow flex items-center gap-2">
                          <CircleHelp size={15} aria-hidden="true" />Answer
                        </h3>
                        {turn.context_available ? null : (
                          <p className="mt-2 border-l-2 border-amber-500 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-950">
                            Some workspace access has changed. This saved answer remains in your private transcript but will not be sent to the agent as context.
                          </p>
                        )}
                        <div className="markdown-answer mt-2">
                          <ReactMarkdown
                            remarkPlugins={MARKDOWN_PLUGINS}
                            components={MARKDOWN_COMPONENTS}
                            skipHtml
                          >
                            {turn.answer ?? ''}
                          </ReactMarkdown>
                        </div>
                        {turn.sources.length === 0 ? null : (
                          <details className="mt-3">
                            <summary className="flex cursor-pointer items-center gap-2 text-xs font-medium text-emerald-900">
                              <BookOpenText size={15} aria-hidden="true" />Sources ({turn.sources.length})
                            </summary>
                            <ul className="mt-2 space-y-2">
                              {turn.sources.map((source, index) => {
                                const workspace = workspaces.find((item) => item.id === source.workspace_id)
                                const pages = source.page_numbers.length === 0
                                  ? 'Page unavailable'
                                  : `Page${source.page_numbers.length > 1 ? 's' : ''} ${source.page_numbers.join(', ')}`
                                return (
                                  <li key={source.chunk_id} className="border-l-2 border-stone-300 pl-3 text-xs leading-5 text-stone-600">
                                    <span className="mr-2 font-mono text-emerald-800">[{index + 1}]</span>
                                    <span className="font-medium text-stone-800">{source.filename ?? 'Workspace document'}</span>
                                    <span className="ml-2">{workspace?.name ?? 'Workspace'} · {pages}</span>
                                    {source.section_path.length === 0 ? null : (
                                      <span className="block">{source.section_path.join(' / ')}</span>
                                    )}
                                  </li>
                                )
                              })}
                            </ul>
                          </details>
                        )}
                      </div>
                    )}
                  </article>
                ))}
              </div>

              <form className="shrink-0 border-t border-stone-200 bg-white p-4 sm:p-6" onSubmit={submitQuestion}>
                {eligibleWorkspaces.length === 0 ? (
                  <p className="mb-3 flex items-center gap-2 text-sm text-amber-900">
                    <ShieldCheck size={16} aria-hidden="true" />Document questions need a searchable workspace membership; greetings are still available.
                  </p>
                ) : null}
                <div className="flex items-center gap-2">
                  <label htmlFor="context-question" className="sr-only">Your message</label>
                  <input
                    id="context-question"
                    type="text"
                    className="field-input h-12 min-w-0 flex-1"
                    value={question}
                    maxLength={2000}
                    placeholder="Ask a question…"
                    disabled={submitting || loadingTurns}
                    onChange={(event) => setQuestion(event.target.value)}
                  />
                  <button
                    type="submit"
                    className="button-primary h-12 shrink-0 px-4"
                    aria-label={submitting ? 'Sending message' : 'Send message'}
                    disabled={submitting || loadingTurns || question.trim() === ''}
                  >
                    {submitting ? <LoaderCircle className="animate-spin" size={16} /> : <Send size={16} />}
                    <span>{submitting ? 'Sending…' : 'Send'}</span>
                  </button>
                </div>
              </form>
            </>
          )}
        </section>
      </div>
    </div>
  )
}

interface ConversationDocument {
  key: string
  workspace_id: string
  workspace_name: string
  document_id: string
  filename: string | null
}

function downloadableConversationDocuments(
  turns: AgentConversationTurn[],
  workspaces: Workspace[],
): ConversationDocument[] {
  const workspaceById = new Map(workspaces.map((workspace) => [workspace.id, workspace]))
  const documents = new Map<string, ConversationDocument>()

  for (const turn of turns) {
    if (!turn.context_available) continue
    for (const source of turn.sources) {
      const workspace = workspaceById.get(source.workspace_id)
      if (workspace?.can_download !== true) continue

      const key = `${source.workspace_id}:${source.document_id}`
      const existing = documents.get(key)
      if (existing === undefined) {
        documents.set(key, {
          key,
          workspace_id: source.workspace_id,
          workspace_name: workspace.name,
          document_id: source.document_id,
          filename: source.filename,
        })
      } else if (existing.filename === null && source.filename !== null) {
        existing.filename = source.filename
      }
    }
  }

  return [...documents.values()].sort((left, right) =>
    (left.filename ?? left.document_id).localeCompare(right.filename ?? right.document_id),
  )
}

function createRequestId(): string {
  const bytes = new Uint8Array(16)
  if (globalThis.crypto?.getRandomValues !== undefined) {
    globalThis.crypto.getRandomValues(bytes)
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256)
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
