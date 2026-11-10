import { useEffect, useRef } from 'react'
import { CirclePlus, Trash2, X } from 'lucide-react'
import type { AgentConversation } from '../hooks/useAgentConversation'

interface ChatHistoryDrawerProps {
  open: boolean
  onClose: () => void
  conversation: AgentConversation
}

/**
 * The chat archive, kept out of the reading column until asked for.
 *
 * The transcript is the work; history is a navigation aid. It opens as a
 * drawer over the left edge rather than permanently narrowing the answer.
 */
export function ChatHistoryDrawer({ open, onClose, conversation }: ChatHistoryDrawerProps) {
  const closeRef = useRef<HTMLButtonElement | null>(null)
  const { sessions, activeId, selectSession, deleteSession, newConversation } = conversation
  const orderedSessions = [...sessions].sort((a, b) =>
    a.updatedAt < b.updatedAt ? 1 : -1,
  )

  useEffect(() => {
    if (!open) return
    closeRef.current?.focus()

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }

    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [open, onClose])

  if (!open) return null

  return (
    <>
      <button
        type="button"
        className="chat-history-scrim"
        aria-label="Dismiss chat history overlay"
        onClick={onClose}
      />
      <aside
        id="chat-history-drawer"
        className="chat-history-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="Chat history"
      >
        <div className="chat-history-header">
          <p className="chat-history-title">Chat history</p>
          <button
            ref={closeRef}
            type="button"
            className="chat-history-close"
            aria-label="Close chat history"
            onClick={onClose}
          >
            <X aria-hidden="true" size={15} />
          </button>
        </div>

        <button
          type="button"
          className="chat-history-new"
          aria-label="Start a new conversation from history"
          onClick={() => {
            newConversation()
            onClose()
          }}
        >
          <CirclePlus aria-hidden="true" size={13} />
          New chat
        </button>

        {orderedSessions.length === 0 ? (
          <p className="chat-history-empty">
            Conversations you start are saved in this browser and listed here.
          </p>
        ) : (
          <ul aria-label="Chat sessions" className="chat-history-list">
            {orderedSessions.map((session) => {
              const active = session.id === activeId
              return (
                <li key={session.id} className="chat-history-entry">
                  <button
                    type="button"
                    className="chat-history-item"
                    aria-current={active ? 'page' : undefined}
                    onClick={() => {
                      selectSession(session.id)
                      onClose()
                    }}
                  >
                    <span className="chat-history-item-title">{session.title}</span>
                    <span className="chat-history-item-date">
                      {new Date(session.updatedAt).toLocaleDateString()}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="chat-history-delete"
                    aria-label={`Delete conversation: ${session.title}`}
                    onClick={(event) => {
                      event.stopPropagation()
                      deleteSession(session.id)
                    }}
                  >
                    <Trash2 aria-hidden="true" size={12} />
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </aside>
    </>
  )
}
