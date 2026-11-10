import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import type { AgentConversation } from '../hooks/useAgentConversation'
import { ChatHistoryPanel } from './ChatHistoryPanel'

interface ChatHistoryDrawerProps {
  open: boolean
  onClose: () => void
  conversation: AgentConversation
}

/**
 * The chat archive on a narrow screen.
 *
 * There is no room for a permanent rail, so the same panel opens over the
 * transcript and closes as soon as a session is picked.
 */
export function ChatHistoryDrawer({ open, onClose, conversation }: ChatHistoryDrawerProps) {
  const closeRef = useRef<HTMLButtonElement | null>(null)

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

        <ChatHistoryPanel conversation={conversation} onDone={onClose} />
      </aside>
    </>
  )
}
