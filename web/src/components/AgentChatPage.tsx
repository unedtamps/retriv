import { lazy, Suspense, useEffect, useRef } from 'react'
import { CirclePlus, CornerDownLeft, Sparkles, Square, Wrench } from 'lucide-react'
import type { AgentConversation } from '../hooks/useAgentConversation'

const MarkdownText = lazy(() => import('./MarkdownText').then((m) => ({ default: m.MarkdownText })))

const SUGGESTIONS = [
  'What is in this workspace?',
  'Summarize the most recent report',
  'Which rows have the highest amounts?',
]

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
  const { items, draft, setDraft, busy, send, stop, newConversation } = conversation
  const scrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const scroller = scrollRef.current
    if (scroller !== null) scroller.scrollTop = scroller.scrollHeight
  }, [items, busy])

  const empty = items.length === 0

  const composer = (
    <form
        className="flex shrink-0 items-end gap-2 rounded-xl border border-line bg-surface px-2 py-1.5 shadow-sm"
        onSubmit={(event) => {
          event.preventDefault()
          void send()
        }}
      >
        <textarea
          className="max-h-48 min-h-[2.75rem] flex-1 resize-none bg-transparent px-2 py-2 text-sm text-ink outline-none placeholder:text-ink-faint"
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
    )

  return (
    <div className="mx-auto flex h-[calc(100dvh-4rem)] max-w-4xl flex-col px-4 pb-3 sm:px-6">
      <style>{MD_STYLES}</style>
      <header className="flex items-baseline justify-between gap-3 border-b border-line py-4">
        <div>
          <p className="eyebrow">Across your workspaces</p>
          <h1 className="font-display text-xl font-medium tracking-[-0.02em] text-ink">Chat</h1>
        </div>
        <div className="flex items-center gap-3">
          <p className="hidden text-xs text-ink-faint sm:block">
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

      <div ref={scrollRef} className="scrollbar-none min-h-0 flex-1 overflow-y-auto">
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
                  className="rounded-full border border-line bg-surface px-3 py-1.5 text-xs text-ink-muted hover:bg-hover"
                  onClick={() => setDraft(suggestion)}
                >
                  {suggestion}
                </button>
              ))}
            </div>
            <div className="w-full max-w-2xl text-left">{composer}</div>
          </div>
        ) : (
          <div className="space-y-3 pb-6 pt-4">
            {items.map((item, index) => {
              if (item.kind === 'user') {
                return (
                  <div key={index} className="flex justify-end">
                    <p className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent px-3.5 py-2 text-sm text-white">
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
                      {Object.keys(call.arguments).length > 0 ? (
                        <p className="mt-0.5 truncate font-mono text-[0.65rem] text-ink-muted">
                          {Object.entries(call.arguments)
                            .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
                            .join('  ')}
                        </p>
                      ) : null}
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

      {empty ? null : composer}
    </div>
  )
}

const MD_STYLES = String.raw`
.scrollbar-none { scrollbar-width: none; }
.scrollbar-none::-webkit-scrollbar { display: none; }
.retriv-md > :first-child { margin-top: 0; }
.retriv-md > :last-child { margin-bottom: 0; }
.retriv-md p { margin: 0.5rem 0; }
.retriv-md h1, .retriv-md h2, .retriv-md h3, .retriv-md h4 {
  font-family: var(--font-display, inherit);
  font-weight: 500;
  color: var(--color-ink, #16202c);
  margin: 1rem 0 0.35rem;
  line-height: 1.3;
}
.retriv-md h1 { font-size: 1.05rem; }
.retriv-md h2 { font-size: 1rem; }
.retriv-md h3, .retriv-md h4 { font-size: 0.9rem; }
.retriv-md ul, .retriv-md ol { margin: 0.5rem 0; padding-left: 1.25rem; }
.retriv-md li { margin: 0.2rem 0; }
.retriv-md ul li { list-style: disc; }
.retriv-md ol li { list-style: decimal; }
.retriv-md code {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.8em;
  background: var(--color-muted, #eef2f7);
  border-radius: 4px;
  padding: 0.1rem 0.35rem;
}
.retriv-md pre {
  background: var(--color-sunken, #f4f7fa);
  border: 1px solid var(--color-line, #d8dee7);
  border-radius: 8px;
  padding: 0.7rem 0.9rem;
  overflow-x: auto;
  margin: 0.6rem 0;
}
.retriv-md pre code { background: transparent; padding: 0; }
.retriv-md table {
  border-collapse: collapse;
  margin: 0.6rem 0;
  font-variant-numeric: tabular-nums;
  display: block;
  overflow-x: auto;
  max-width: 100%;
}
.retriv-md th, .retriv-md td {
  border: 1px solid var(--color-line, #d8dee7);
  padding: 0.3rem 0.6rem;
  text-align: left;
}
.retriv-md th { background: var(--color-paper, #f4f7fa); font-weight: 600; }
.retriv-md blockquote {
  border-left: 3px solid var(--color-line, #d8dee7);
  margin: 0.6rem 0;
  padding: 0.1rem 0 0.1rem 0.8rem;
  color: var(--color-ink-muted, #5a6675);
}
.retriv-md hr { border: 0; border-top: 1px solid var(--color-line, #d8dee7); margin: 0.8rem 0; }
.retriv-md a { color: var(--color-accent-ink, #1f5fa8); text-decoration: underline; }
`
