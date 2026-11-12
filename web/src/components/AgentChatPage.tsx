import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { CirclePlus, PanelLeft, Square } from 'lucide-react'
import type { AgentConversation, ChatItem, ToolStep } from '../hooks/useAgentConversation'
import type { ProcessingRuns } from '../hooks/useProcessingRuns'
import type { AgentChatSource, ProcessingRun, Workspace } from '../types'
import { useIsDesktop } from '../hooks/useIsDesktop'
import { ChatAnswerCard, type CitationTarget } from './ChatAnswerCard'
import { ChatHistoryDrawer } from './ChatHistoryDrawer'
import { ChatSidebar } from './ChatSidebar'
import { ChatProcessingBanner } from './ChatProcessingBanner'

const SIDEBAR_KEY = 'retriv.chat.sidebar'

const SUGGESTIONS = [
  'What is in this workspace?',
  'Summarize the most recent report',
  'Which rows have the highest amounts?',
]

interface TurnGroup {
  key: number
  question: string | null
  calls: ToolStep[]
  texts: string[]
  sources: AgentChatSource[]
  streaming: boolean
  errors: string[]
}

/**
 * Group the flat event list into questions and their answers.
 *
 * The wire format alternates user text, tool activity, and one or more
 * assistant messages. The page needs the final answer and its whole evidence
 * trail together, so the trail can sit below the answer rather than above it.
 */
function groupTurns(items: ChatItem[]): TurnGroup[] {
  const turns: TurnGroup[] = []
  let current: TurnGroup | null = null

  const start = (question: string | null): TurnGroup => {
    const turn: TurnGroup = {
      key: turns.length,
      question,
      calls: [],
      texts: [],
      sources: [],
      streaming: false,
      errors: [],
    }
    turns.push(turn)
    current = turn
    return turn
  }

  for (const item of items) {
    if (item.kind === 'user') {
      start(item.text)
      continue
    }
    const turn: TurnGroup = current ?? start(null)
    if (item.kind === 'activity') {
      turn.calls.push(...item.calls)
      continue
    }
    if (item.kind === 'assistant') {
      turn.texts.push(item.text)
      if (item.sources !== undefined) turn.sources = item.sources
      turn.streaming = item.streaming === true
      continue
    }
    if (item.kind === 'error') {
      turn.errors.push(item.text)
    }
  }
  return turns
}

/**
 * The agent chat as a document room.
 *
 * The transcript is the work surface; the archive is a drawer opened from the
 * left edge, so a long answer is never squeezed by a permanent sidebar. The
 * composer stays on the lower edge and the active pipeline run stays above the
 * conversation instead of hiding in a notification popover.
 */
export function AgentChatPage({
  conversation,
  runs,
  workspace,
  onOpenSource,
}: {
  conversation: AgentConversation
  runs: ProcessingRuns
  workspace: Workspace
  onOpenSource?: (target: CitationTarget) => void
}) {
  const { items, draft, setDraft, busy, send, stop, newConversation } = conversation
  const isDesktop = useIsDesktop()
  const [sidebarOpen, setSidebarOpen] = useState(() => {
    if (typeof localStorage === 'undefined') return true
    return localStorage.getItem(SIDEBAR_KEY) !== 'collapsed'
  })
  const [historyOpen, setHistoryOpen] = useState(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const turns = useMemo(() => groupTurns(items), [items])
  const empty = items.length === 0
  const closeHistory = useCallback(() => setHistoryOpen(false), [])
  const activeRun: ProcessingRun | null =
    runs.runs.find((run) => run.workspace_id === workspace.id) ?? null

  useEffect(() => {
    const scroller = scrollRef.current
    if (scroller !== null) scroller.scrollTop = scroller.scrollHeight
  }, [items, busy])

  useEffect(() => {
    localStorage.setItem(SIDEBAR_KEY, sidebarOpen ? 'open' : 'collapsed')
  }, [sidebarOpen])

  // The chat is its own viewport: the page around it stays put and only the
  // transcript, sidebar list and composer scroll area move.
  useEffect(() => {
    document.body.classList.add('chat-viewport')
    return () => document.body.classList.remove('chat-viewport')
  }, [])

  const composer = (
    <form
      className="chat-composer"
      onSubmit={(event) => {
        event.preventDefault()
        void send()
      }}
    >
      <textarea
        className="chat-input"
        rows={1}
        autoFocus
        placeholder="Ask this workspace…"
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
        <button type="button" className="chat-stop" onClick={stop}>
          <Square aria-hidden="true" size={12} />
          Stop
        </button>
      ) : (
        <button
          type="submit"
          className="chat-send"
          disabled={draft.trim() === ''}
        >
          Send
        </button>
      )}
    </form>
  )

  return (
    <div className="chat-room">
      <h1 className="sr-only">Chat</h1>

      {isDesktop ? (
        <ChatSidebar
          conversation={conversation}
          collapsed={!sidebarOpen}
          onToggle={() => setSidebarOpen((open) => !open)}
        />
      ) : null}

      <div className="chat-main">
        {isDesktop ? null : (
          <div className="chat-topbar">
            <button
              type="button"
              className="chat-history-tab"
              aria-controls="chat-history-drawer"
              aria-expanded={historyOpen}
              onClick={() => setHistoryOpen(true)}
            >
              <PanelLeft aria-hidden="true" size={13} />
              History
            </button>

            <button
              type="button"
              className="chat-new-task"
              aria-label="New task"
              onClick={() => {
                newConversation()
                setHistoryOpen(false)
              }}
            >
              <CirclePlus aria-hidden="true" size={13} />
              New task
            </button>
          </div>
        )}

        <div ref={scrollRef} data-testid="chat-transcript" className="chat-scroll">
          <div className="chat-column">
            {activeRun === null ? null : <ChatProcessingBanner run={activeRun} />}

            {empty ? (
              <div className="chat-empty">
                <h2 className="chat-empty-title">Ask this workspace</h2>
                <p className="chat-empty-copy">
                  The agent searches, reads and queries the documents in this workspace, and
                  answers with the sources it used.
                </p>
                <div className="chat-suggestions">
                  {SUGGESTIONS.map((suggestion) => (
                    <button
                      key={suggestion}
                      type="button"
                      className="chat-suggestion"
                      onClick={() => setDraft(suggestion)}
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              turns.map((turn) => {
                const isLiveTurn = busy && turn.key === turns.length - 1
                const showAnswer =
                  turn.texts.length > 0 ||
                  turn.calls.length > 0 ||
                  turn.errors.length > 0 ||
                  isLiveTurn
                return (
                  <div className="chat-turn" key={turn.key}>
                    {turn.question === null ? null : (
                      <div className="chat-user-row">
                        <p className="chat-user-bubble">{turn.question}</p>
                      </div>
                    )}
                    {showAnswer ? (
                      <ChatAnswerCard
                        calls={turn.calls}
                        texts={turn.texts}
                        sources={turn.sources}
                        streaming={turn.streaming}
                        busy={isLiveTurn}
                        errors={turn.errors}
                        onOpenSource={onOpenSource}
                      />
                    ) : null}
                  </div>
                )
              })
            )}
          </div>
        </div>

        <div className="chat-composer-area">
          <div className="chat-composer-wrap">{composer}</div>
        </div>
      </div>

      {isDesktop ? null : (
        <ChatHistoryDrawer
          open={historyOpen}
          onClose={closeHistory}
          conversation={conversation}
        />
      )}
    </div>
  )
}
