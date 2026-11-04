import { useEffect, useRef } from 'react'
import { CornerDownLeft, Sparkles, Square, Wrench } from 'lucide-react'
import type { AgentConversation } from '../hooks/useAgentConversation'
import type { Workspace } from '../types'

const SUGGESTIONS = [
  'What is in this workspace?',
  'Summarize the most recent report',
  'Which rows have the highest amounts?',
]

/**
 * The workspace agent, as a page.
 *
 * A centered column with the conversation in the middle and the composer
 * pinned underneath, because asking the workspace questions is the front door
 * of the product now: this view is where every signed-in session starts. The
 * conversation itself is owned by the hook above this page, so wandering off
 * to Documents and back does not erase it -- but it is still ephemeral: the
 * server keeps nothing, and a refresh starts over.
 */
export function AgentChatPage({
  workspace,
  conversation,
}: {
  workspace: Workspace
  conversation: AgentConversation
}) {
  const { items, draft, setDraft, busy, send, stop } = conversation
  const scrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const scroller = scrollRef.current
    if (scroller !== null) scroller.scrollTop = scroller.scrollHeight
  }, [items, busy])

  const empty = items.length === 0

  return (
    <div className="mx-auto flex h-[calc(100dvh-4rem)] max-w-3xl flex-col px-4 sm:px-6">
      <header className="flex items-baseline justify-between gap-3 border-b border-line py-4">
        <div>
          <p className="eyebrow">{workspace.name}</p>
          <h1 className="font-display text-xl font-medium tracking-[-0.02em] text-ink">Chat</h1>
        </div>
        <p className="hidden text-xs text-ink-faint sm:block">
          Ephemeral — a refresh starts a new conversation
        </p>
      </header>

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto py-5">
        {empty ? (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <Sparkles aria-hidden="true" size={22} className="text-ink-faint" strokeWidth={1.6} />
            <div>
              <p className="text-sm font-medium text-ink">Ask this workspace</p>
              <p className="mx-auto mt-1 max-w-sm text-sm leading-relaxed text-ink-muted">
                The agent searches, reads and queries the documents here, and answers with
                citations.
              </p>
            </div>
            <div className="flex flex-wrap justify-center gap-2">
              {SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  className="rounded-full border border-line bg-surface px-3 py-1.5 text-xs text-ink-muted hover:bg-hover"
                  onClick={() => setDraft(suggestion)}
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            {items.map((item, index) => {
              if (item.kind === 'user') {
                return (
                  <div key={index} className="flex justify-end">
                    <p className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent px-3.5 py-2 text-sm">
                      {item.text}
                    </p>
                  </div>
                )
              }
              if (item.kind === 'assistant') {
                return (
                  <p key={index} className="whitespace-pre-wrap text-sm leading-relaxed text-ink">
                    {item.text}
                  </p>
                )
              }
              if (item.kind === 'error') {
                return (
                  <p
                    key={index}
                    className="rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger"
                  >
                    {item.text}
                  </p>
                )
              }
              if (item.calls.length === 0) {
                return (
                  <p key={index} className="text-xs text-ink-faint" role="status">
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
            })}
          </div>
        )}
      </div>

      <form
        className="flex items-end gap-2 rounded-xl border border-line bg-surface p-2 shadow-sm"
        onSubmit={(event) => {
          event.preventDefault()
          void send()
        }}
      >
        <textarea
          className="max-h-28 min-h-[2.5rem] flex-1 resize-none bg-transparent px-2 py-1.5 text-sm text-ink outline-none placeholder:text-ink-faint"
          rows={1}
          placeholder={`Ask about ${workspace.name}…`}
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
          <button type="button" className="btn btn-sm" onClick={stop}>
            <Square aria-hidden="true" size={13} />
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
    </div>
  )
}
