import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { ChevronDown, CirclePlus, CornerDownLeft, Sparkles, Square, Trash2, Wrench } from 'lucide-react'
import type { AgentConversation, ToolStep } from '../hooks/useAgentConversation'

const MarkdownText = lazy(() => import('./MarkdownText').then((m) => ({ default: m.MarkdownText })))

const SUGGESTIONS = [
  'What is in this workspace?',
  'Summarize the most recent report',
  'Which rows have the highest amounts?',
]

/**
 * The tool trail, folded into one line.
 *
 * A turn can run a dozen tools, and a full card per call buried the answer
 * it produced. The summary names the call running now (re-keyed so the name
 * animates when the trail moves on) and counts the rest; the disclosure
 * opens the whole trail when someone wants to audit it.
 */
function ToolActivity({ calls, busy }: { calls: ToolStep[]; busy: boolean }) {
  const [open, setOpen] = useState(false)
  if (calls.length === 0) {
    return (
      <p className="text-xs text-ink-faint" role="status">
        Working…
      </p>
    )
  }
  const latest = calls[calls.length - 1]
  const running = busy && latest.preview === ''
  return (
    <div className="rounded-control border border-line bg-sunken text-xs">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left"
      >
        <Wrench aria-hidden="true" size={11} className="shrink-0 text-ink-faint" />
        <span key={`${calls.length}-${latest.name}`} className="tool-swap font-mono text-[0.7rem] font-medium text-ink">
          {latest.name}
        </span>
        <span className="font-mono text-ink-faint">
          {calls.length} tool call{calls.length === 1 ? '' : 's'}
        </span>
        {running ? (
          <span className="flex items-center gap-1 text-ink-faint">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
            running
          </span>
        ) : null}
        <ChevronDown
          aria-hidden="true"
          size={12}
          className={`ml-auto shrink-0 text-ink-faint transition-transform duration-200 ${
            open ? 'rotate-180' : ''
          }`}
        />
      </button>
      {open ? (
        <div className="roster-enter space-y-1.5 border-t border-line px-2.5 py-1.5">
          {calls.map((call, index) => {
            const argumentsText = Object.entries(call.arguments)
              .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
              .join('  ')
            const callRunning = busy && index === calls.length - 1 && call.preview === ''
            return (
              <div key={index} className="text-[0.7rem]">
                <div className="flex items-baseline gap-2">
                  <span className="shrink-0 font-medium text-ink">{call.name}</span>
                  <span className="min-w-0 flex-1 truncate font-mono text-ink-faint">
                    {argumentsText}
                  </span>
                  {callRunning ? (
                    <span className="shrink-0 text-ink-faint">running</span>
                  ) : call.truncated ? (
                    <span className="shrink-0 text-ink-faint">truncated</span>
                  ) : null}
                </div>
                {call.preview !== '' ? (
                  <p className="mt-0.5 line-clamp-1 whitespace-pre-wrap text-[0.65rem] text-ink-faint">
                    {call.preview}
                  </p>
                ) : null}
              </div>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}

/**
 * The agent, as a page.
 *
 * A centered column with the conversation in the middle and the composer
 * pinned underneath, because asking the workspace questions is the front door
 * of the product now: this view is where every signed-in session starts. The
 * conversation itself is owned by the hook above this page, so wandering off
 * to Documents and back does not erase it -- but it is still ephemeral: the
 * server keeps nothing, and a refresh starts over.
 */
export function AgentChatPage({ conversation }: { conversation: AgentConversation }) {
  const {
    items,
    draft,
    setDraft,
    busy,
    send,
    stop,
    newConversation,
    sessions,
    activeId,
    selectSession,
    deleteSession,
  } = conversation
  const orderedSessions = [...sessions].sort((a, b) =>
    a.updatedAt < b.updatedAt ? 1 : -1,
  )
  const scrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const scroller = scrollRef.current
    if (scroller !== null) scroller.scrollTop = scroller.scrollHeight
  }, [items, busy])

  const empty = items.length === 0

  const composer = (
    <form
      className="mx-auto flex w-full max-w-3xl items-end gap-2 rounded-control border border-line bg-sunken px-2 py-1.5 transition-colors focus-within:border-accent focus-within:bg-surface"
      onSubmit={(event) => {
        event.preventDefault()
        void send()
      }}
    >
        <textarea
          className="max-h-48 min-h-[2.5rem] flex-1 resize-none bg-transparent px-2 py-1.5 text-sm text-ink outline-none placeholder:text-ink-faint"
          rows={1}
          autoFocus
          placeholder="Ask across your workspaces…"
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
          <button type="button" className="btn btn-sm btn-danger" onClick={stop}>
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
  )

  return (
    <div className="flex h-[calc(100dvh-5rem)] min-h-0 gap-3 p-3">
      <aside className="hidden w-60 shrink-0 flex-col overflow-hidden rounded-card border border-line bg-surface md:flex">
        <div className="px-3 pt-4">
          <button type="button" className="btn btn-sm w-full justify-center" onClick={newConversation}>
            <CirclePlus aria-hidden="true" size={14} />
            New chat
          </button>
        </div>
        {orderedSessions.length === 0 ? (
          <p className="px-3 pt-3 text-xs leading-5 text-ink-faint">
            Conversations you start are listed here, saved in this browser only.
          </p>
        ) : (
          <ul aria-label="Chat sessions" className="scrollbar-none mt-3 min-h-0 flex-1 space-y-1 overflow-y-auto px-2 pb-3">
            {orderedSessions.map((session) => {
              const active = session.id === activeId
              return (
                <li key={session.id} className="group relative">
                  <button
                    type="button"
                    className={`w-full rounded-control px-2.5 py-2 pr-7 text-left transition-colors ${
                      active
                        ? 'bg-sunken text-ink shadow-[inset_2px_0_0_var(--color-accent)]'
                        : 'hover:bg-sunken'
                    }`}
                    aria-current={active ? 'page' : undefined}
                    onClick={() => selectSession(session.id)}
                  >
                    <span className="block truncate text-xs font-medium text-ink">
                      {session.title}
                    </span>
                    <span className="block font-mono text-[0.65rem] text-ink-faint">
                      {new Date(session.updatedAt).toLocaleDateString()}
                    </span>
                  </button>
                  <button
                    type="button"
                    aria-label={`Delete conversation: ${session.title}`}
                    className="absolute right-1.5 top-2 hidden rounded p-1 text-ink-faint hover:bg-surface hover:text-ink group-hover:block"
                    onClick={() => deleteSession(session.id)}
                  >
                    <Trash2 aria-hidden="true" size={12} />
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </aside>

      <div className="mx-auto flex min-w-0 flex-1 max-w-4xl flex-col overflow-hidden rounded-card border border-line bg-surface">
        <header className="flex shrink-0 items-baseline justify-between gap-3 border-b border-line px-4 py-3 sm:px-6">
          <div>
            <p className="eyebrow">Across your workspaces</p>
            <h1 className="font-condensed text-base font-bold uppercase tracking-[0.08em] text-ink">
              Chat
            </h1>
          </div>
          <div className="flex items-center gap-3">
            <p className="hidden font-mono text-[0.65rem] text-ink-faint sm:block">
              Saved in this browser — sign out clears it
            </p>
            <button
              type="button"
              className="btn btn-sm"
              aria-label="Start a new conversation"
              onClick={newConversation}
            >
              <CirclePlus aria-hidden="true" size={14} />
              New chat
            </button>
          </div>
        </header>

        <div ref={scrollRef} data-testid="chat-transcript" className="scrollbar-none min-h-0 flex-1 overflow-y-auto px-4 sm:px-6">
          {empty ? (
            <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
              <Sparkles aria-hidden="true" size={22} className="text-ink-faint" strokeWidth={1.6} />
              <div>
                <p className="text-sm font-medium text-ink">Ask across your workspaces</p>
                <p className="mx-auto mt-1 max-w-sm text-sm leading-relaxed text-ink-muted">
                  The agent searches, reads and queries the documents in every workspace you can
                  read, and answers with citations.
                </p>
              </div>
              <div className="flex flex-wrap justify-center gap-2">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    className="rounded-stamp border border-line bg-sunken px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-line-strong hover:bg-surface hover:text-ink"
                    onClick={() => setDraft(suggestion)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
              <div className="w-full max-w-2xl text-left">{composer}</div>
            </div>
          ) : (
            <div className="mx-auto max-w-3xl space-y-3 pb-6 pt-4">
              {items.map((item, index) => {
                if (item.kind === 'user') {
                  return (
                    <div key={index} className="flex justify-end">
                      <p className="max-w-[85%] whitespace-pre-wrap rounded-control bg-ink px-3.5 py-2 text-sm font-medium text-surface">
                        {item.text}
                      </p>
                    </div>
                  )
                }
                if (item.kind === 'assistant') {
                  // While tokens are still arriving the text renders plainly:
                  // re-parsing markdown on every delta flickers half-built
                  // tables and code fences. `final` marks the item complete,
                  // and only then is the markdown pipeline paid for.
                  if (item.streaming === true) {
                    return (
                      <p key={index} className="whitespace-pre-wrap text-sm leading-relaxed text-ink">
                        {item.text}
                        <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-ink-muted align-middle" />
                      </p>
                    )
                  }
                  return (
                    <div key={index} className="text-sm leading-relaxed text-ink">
                      <Suspense
                        fallback={<p className="whitespace-pre-wrap">{item.text}</p>}
                      >
                        <MarkdownText text={item.text} />
                      </Suspense>
                    </div>
                  )
                }
                if (item.kind === 'error') {
                  return (
                    <p
                      key={index}
                      className="rounded-control border border-danger/40 bg-danger-soft px-3 py-2 text-sm text-danger"
                    >
                      {item.text}
                    </p>
                  )
                }
                return <ToolActivity key={index} calls={item.calls} busy={busy} />
              })}
            </div>
          )}
        </div>

        {empty ? null : (
          <div className="shrink-0 border-t border-line px-4 py-3 sm:px-6">{composer}</div>
        )}
      </div>
    </div>
  )
}
