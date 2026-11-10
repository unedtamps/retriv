import { CirclePlus, Trash2 } from 'lucide-react'
import type { AgentConversation } from '../hooks/useAgentConversation'

interface ChatHistoryPanelProps {
  conversation: AgentConversation
  /** Called after a navigation action; the narrow drawer closes here. */
  onDone?: () => void
}

/**
 * The session archive: the new-task action and every conversation this
 * browser remembers.
 *
 * Shared by the permanent sidebar and the narrow-screen drawer so the two
 * never drift apart.
 */
export function ChatHistoryPanel({ conversation, onDone }: ChatHistoryPanelProps) {
  const { sessions, activeId, selectSession, deleteSession, newConversation } = conversation
  const orderedSessions = [...sessions].sort((a, b) =>
    a.updatedAt < b.updatedAt ? 1 : -1,
  )

  return (
    <div className="chat-history-panel">
      <button
        type="button"
        className="chat-history-new"
        aria-label="New task"
        onClick={() => {
          newConversation()
          onDone?.()
        }}
      >
        <CirclePlus aria-hidden="true" size={14} />
        New task
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
                    onDone?.()
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
    </div>
  )
}
