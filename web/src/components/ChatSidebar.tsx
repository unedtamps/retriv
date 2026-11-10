import { CirclePlus, PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import type { AgentConversation } from '../hooks/useAgentConversation'
import { ChatHistoryPanel } from './ChatHistoryPanel'

interface ChatSidebarProps {
  conversation: AgentConversation
  collapsed: boolean
  onToggle: () => void
}

/**
 * The archive as a permanent, collapsible rail.
 *
 * Open, it names the work: a New task button and the sessions this browser
 * remembers. Collapsed, it keeps both actions reachable as icons while the
 * transcript takes the width.
 */
export function ChatSidebar({ conversation, collapsed, onToggle }: ChatSidebarProps) {
  return (
    <aside
      className={`chat-sidebar${collapsed ? ' chat-sidebar-collapsed' : ''}`}
      aria-label="Chat history"
    >
      <div className="chat-sidebar-head">
        {collapsed ? null : <p className="chat-sidebar-title">History</p>}
        <button
          type="button"
          className="chat-sidebar-toggle"
          aria-label={collapsed ? 'Show chat history' : 'Collapse chat history'}
          aria-expanded={!collapsed}
          onClick={onToggle}
        >
          {collapsed ? (
            <PanelLeftOpen aria-hidden="true" size={15} />
          ) : (
            <PanelLeftClose aria-hidden="true" size={15} />
          )}
        </button>
      </div>

      {collapsed ? (
        <button
          type="button"
          className="chat-sidebar-new"
          aria-label="New task"
          onClick={conversation.newConversation}
        >
          <CirclePlus aria-hidden="true" size={15} />
        </button>
      ) : (
        <ChatHistoryPanel conversation={conversation} />
      )}
    </aside>
  )
}
