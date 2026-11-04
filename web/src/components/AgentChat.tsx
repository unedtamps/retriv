import { useEffect, useRef, useState } from 'react'
import { CornerDownLeft, MessageSquareText, Sparkles, Wrench, X } from 'lucide-react'
import { getErrorMessage, isUnauthorized, streamAgentChat } from '../api'
import type { AgentChatEvent, AgentChatHistoryMessage, Workspace } from '../types'

interface ToolStep {
  name: string
  preview: string
  truncated: boolean
}

type ChatItem =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string }
  | { kind: 'activity'; calls: ToolStep[] }
  | { kind: 'error'; text: string }

const HISTORY_LIMIT = 20

function historyOf(items: ChatItem[]): AgentChatHistoryMessage[] {
  return items
    .filter((item): item is Extract<ChatItem, { kind: 'user' | 'assistant' }> =>
      item.kind === 'user' || item.kind === 'assistant')
    .slice(-HISTORY_LIMIT)
    .map((item) => ({ role: item.kind, content: item.text }))
}

/**
 * The workspace agent, as a floating panel.
 *
 * The conversation lives in this component's state only: a refresh clears
 * it, and the server keeps nothing. While a run is in flight the panel
 * shows the tool calls as they happen -- each call's name and a short
 * preview of what came back -- so waiting on the model reads as progress
 * rather than silence.
 */
export function AgentChat({
  workspace,
  onUnauthorized,
}: {
  workspace: Workspace
  onUnauthorized: () => void
}) {
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState<ChatItem[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  // One stable id per conversation: providers that key routing or prompt
  // caching on it (OpenCode Go requires it) see the same conversation the
  // whole panel lives for. Ephemeral by design -- a refresh starts a new one.
  const sessionIdRef = useRef<string>(crypto.randomUUID())

  useEffect(() => {
    const scroller = scrollRef.current
    if (scroller !== null) scroller.scrollTop = scroller.scrollHeight
  }, [items, busy])

  useEffect(() => () => abortRef.current?.abort(), [])

  const send = async () => {
    const message = draft.trim()
    if (message === '' || busy) return
    setDraft('')
    setBusy(true)
    const withQuestion: ChatItem[] = [...items, { kind: 'user', text: message }]
    const activity: ChatItem = { kind: 'activity', calls: [] }
    setItems([...withQuestion, activity])
    const controller = new AbortController()
    abortRef.current = controller

    const patchActivity = (patch: (calls: ToolStep[]) => ToolStep[]) => {
      setItems((current) =>
        current.map((item) =>
          item === activity ? { kind: 'activity', calls: patch(item.calls) } : item,
        ),
      )
    }

    const onEvent = (event: AgentChatEvent) => {
      if (event.type === 'tool_call') {
        patchActivity((calls) => [
          ...calls,
          {
            name: event.name ?? 'tool',
            preview: '',
            truncated: false,
          },
        ])
        return
      }
      if (event.type === 'tool_result') {
        patchActivity((calls) =>
          calls.map((call, index) =>
            index === calls.length - 1
              ? { ...call, preview: event.preview ?? '', truncated: event.truncated ?? false }
              : call,
          ),
        )
        return
      }
      if (event.type === 'message') {
        setItems((current) => [
          ...current,
          { kind: 'assistant', text: event.content ?? '' },
        ])
        return
      }
      if (event.type === 'final') {
        setItems((current) => [
          ...current,
          { kind: 'assistant', text: event.content ?? '' },
        ])
        return
      }
      if (event.type === 'error') {
        setItems((current) => [
          ...current,
          { kind: 'error', text: event.message ?? 'The agent run failed.' },
        ])
      }
    }

    try {
      await streamAgentChat({
        workspaceId: workspace.id,
        message,
        history: historyOf(items),
        sessionId: sessionIdRef.current,
        signal: controller.signal,
        onEvent,
      })
    } catch (caught) {
      if (isUnauthorized(caught)) {
        onUnauthorized()
      } else if ((caught as Error).name !== 'AbortError') {
        const detail = getErrorMessage(caught)
        setItems((current) => [...current, { kind: 'error', text: detail }])
      }
    } finally {
      abortRef.current = null
      setBusy(false)
    }
  }

  const empty = items.length === 0

  return (
    <div className="fixed bottom-5 right-5 z-40 flex flex-col items-end gap-3">
      {open ? (
        <section className="flex h-[34rem] w-[24rem] max-w-[calc(100vw-2.5rem)] flex-col overflow-hidden rounded-xl border bg-surface shadow-xl">
          <header className="flex items-center justify-between border-b px-4 py-3">
            <div className="flex min-w-0 items-center gap-2">
              <Sparkles aria-hidden="true" size={15} className="text-ink-faint" />
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">Ask this workspace</p>
                <p className="truncate text-xs text-ink-faint">{workspace.name}</p>
              </div>
            </div>
            <button
              type="button"
              aria-label="Close the agent panel"
              className="rounded p-1 text-ink-faint hover:bg-hover"
              onClick={() => {
                abortRef.current?.abort()
                setOpen(false)
              }}
            >
              <X aria-hidden="true" size={16} />
            </button>
          </header>

          <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
            {empty ? (
              <p className="text-sm leading-relaxed text-ink-faint">
                Ask about the documents in this workspace. The agent searches,
                reads and queries them, and answers with citations.
              </p>
            ) : (
              items.map((item, index) => {
                if (item.kind === 'user') {
                  return (
                    <div key={index} className="flex justify-end">
                      <p className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-accent px-3 py-2 text-sm">
                        {item.text}
                      </p>
                    </div>
                  )
                }
                if (item.kind === 'assistant') {
                  return (
                    <p
                      key={index}
                      className="max-w-[92%] whitespace-pre-wrap text-sm leading-relaxed"
                    >
                      {item.text}
                    </p>
                  )
                }
                if (item.kind === 'error') {
                  return (
                    <p
                      key={index}
                      className="max-w-[92%] rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger"
                    >
                      {item.text}
                    </p>
                  )
                }
                if (item.calls.length === 0) {
                  return (
                    <p key={index} className="text-xs text-ink-faint">
                      Working…
                    </p>
                  )
                }
                return (
                  <div key={index} className="space-y-1.5">
                    {item.calls.map((call, callIndex) => (
                      <div
                        key={callIndex}
                        className="rounded-md border bg-muted/50 px-2.5 py-1.5 text-xs"
                      >
                        <p className="flex items-center gap-1.5 font-medium">
                          <Wrench aria-hidden="true" size={11} className="text-ink-faint" />
                          {call.name}
                          {busy && callIndex === item.calls.length - 1 && call.preview === '' ? (
                            <span className="text-ink-faint">· running</span>
                          ) : null}
                        </p>
                        {call.preview !== '' ? (
                          <p className="mt-1 line-clamp-3 whitespace-pre-wrap text-ink-faint">
                            {call.preview}
                          </p>
                        ) : null}
                        {call.truncated ? (
                          <p className="mt-1 text-ink-faint">Result was truncated.</p>
                        ) : null}
                      </div>
                    ))}
                  </div>
                )
              })
            )}
          </div>

          <form
            className="flex items-end gap-2 border-t px-3 py-3"
            onSubmit={(event) => {
              event.preventDefault()
              void send()
            }}
          >
            <textarea
              className="field max-h-28 min-h-[2.5rem] flex-1 resize-none py-2"
              rows={1}
              placeholder="Ask about this workspace…"
              value={draft}
              maxLength={8000}
              disabled={busy}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  void send()
                }
              }}
            />
            {busy ? (
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => abortRef.current?.abort()}
              >
                Stop
              </button>
            ) : (
              <button
                type="submit"
                className="btn btn-sm btn-primary"
                disabled={draft.trim() === ''}
                aria-label="Send the question"
              >
                <CornerDownLeft aria-hidden="true" size={14} />
              </button>
            )}
          </form>
        </section>
      ) : null}

      <button
        type="button"
        className="btn btn-primary shadow-lg"
        onClick={() => setOpen((current) => !current)}
      >
        <MessageSquareText aria-hidden="true" size={16} />
        Ask
      </button>
    </div>
  )
}
