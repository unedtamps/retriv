import { useEffect, useRef, useState } from 'react'
import { getErrorMessage, isUnauthorized, streamAgentChat } from '../api'
import type { AgentChatEvent, AgentChatHistoryMessage } from '../types'

export interface ToolStep {
  name: string
  preview: string
  truncated: boolean
}

export type ChatItem =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string }
  | { kind: 'activity'; calls: ToolStep[] }
  | { kind: 'error'; text: string }

export const HISTORY_LIMIT = 20

export function historyOf(items: ChatItem[]): AgentChatHistoryMessage[] {
  return items
    .filter((item): item is Extract<ChatItem, { kind: 'user' | 'assistant' }> =>
      item.kind === 'user' || item.kind === 'assistant')
    .slice(-HISTORY_LIMIT)
    .map((item) => ({ role: item.kind, content: item.text }))
}

/**
 * One agent conversation, owned above the page that shows it.
 *
 * The conversation lives in this hook's state only: a refresh clears it, and
 * the server keeps nothing. It is held here rather than inside the chat page
 * because the page mounts and unmounts as the user moves between views, and a
 * conversation that died on every tab switch would make the tab useless.
 *
 * Switching workspaces starts a new conversation. The history is replayed to
 * the model on every turn, and a workspace-A transcript fed to a run whose
 * tools search workspace B would answer from the wrong context.
 */
export function useAgentConversation(
  workspaceId: string,
  onUnauthorized: () => void,
): AgentConversation {
  const [items, setItems] = useState<ChatItem[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  // One stable id per conversation: providers that key routing or prompt
  // caching on it (OpenCode Go requires it) see the same conversation the
  // whole session lives for. Ephemeral by design -- a refresh starts a new one.
  const sessionIdRef = useRef<string>(crypto.randomUUID())
  const workspaceRef = useRef(workspaceId)

  useEffect(() => {
    if (workspaceRef.current === workspaceId) return
    workspaceRef.current = workspaceId
    abortRef.current?.abort()
    sessionIdRef.current = crypto.randomUUID()
    setItems([])
    setDraft('')
    setBusy(false)
  }, [workspaceId])

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
      if (event.type === 'message' || event.type === 'final') {
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
        workspaceId,
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

  const stop = () => abortRef.current?.abort()

  return { items, draft, setDraft, busy, send, stop }
}

export type AgentConversation = ReturnType<typeof useAgentConversation>
