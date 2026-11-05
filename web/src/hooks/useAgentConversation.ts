import { useEffect, useMemo, useRef, useState } from 'react'
import { getErrorMessage, isUnauthorized, streamAgentChat } from '../api'
import { loadChatHistory, saveChatHistory } from '../lib/chatHistoryStore'
import type { AgentChatEvent, AgentChatHistoryMessage } from '../types'

export interface ToolStep {
  name: string
  arguments: Record<string, unknown>
  preview: string
  truncated: boolean
}

export type ChatItem =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string; streaming?: boolean }
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
 * The conversation is held here rather than inside the chat page because the
 * page mounts and unmounts as the user moves between views, and a
 * conversation that died on every tab switch would make the tab useless. It
 * also persists to this browser's storage: the wire protocol replays the
 * recent history on every turn anyway, so what the client remembers is
 * exactly what a continuation needs -- the transcript and the session id the
 * model side keys its caching on. A refresh keeps it; only New chat or sign
 * out ends it.
 */
export function useAgentConversation(onUnauthorized: () => void): AgentConversation {
  const stored = useMemo(() => loadChatHistory(), [])
  const [items, setItems] = useState<ChatItem[]>(stored?.items ?? [])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  // One stable id per conversation: providers that key routing or prompt
  // caching on it (OpenCode Go requires it) see the same conversation the
  // whole session lives for.
  const sessionIdRef = useRef<string>(stored?.sessionId ?? crypto.randomUUID())
  const streamingRef = useRef(false)

  useEffect(() => {
    const timer = setTimeout(() => saveChatHistory(sessionIdRef.current, items), 300)
    return () => clearTimeout(timer)
  }, [items])

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

    const appendDelta = (chunk: string) => {
      if (streamingRef.current) {
        setItems((current) => {
          const last = current[current.length - 1]
          if (last === undefined || last.kind !== 'assistant') {
            return [...current, { kind: 'assistant', text: chunk, streaming: true }]
          }
          return [
            ...current.slice(0, -1),
            { kind: 'assistant', text: last.text + chunk, streaming: true },
          ]
        })
        return
      }
      streamingRef.current = true
      setItems((current) => [
        ...current,
        { kind: 'assistant', text: chunk, streaming: true },
      ])
    }

    const patchActivity = (patch: (calls: ToolStep[]) => ToolStep[]) => {
      setItems((current) =>
        current.map((item) =>
          item === activity ? { kind: 'activity', calls: patch(item.calls) } : item,
        ),
      )
    }

    const onEvent = (event: AgentChatEvent) => {
      if (event.type === 'turn_start') {
        streamingRef.current = false
        return
      }
      if (event.type === 'message_delta') {
        if (event.content) appendDelta(event.content)
        return
      }
      if (event.type === 'tool_call') {
        streamingRef.current = false
        patchActivity((calls) => [
          ...calls,
          {
            name: event.name ?? 'tool',
            arguments: event.arguments ?? {},
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
        streamingRef.current = false
        setItems((current) => {
          const last = current[current.length - 1]
          if (last !== undefined && last.kind === 'assistant' && last.streaming === true) {
            return [
              ...current.slice(0, -1),
              { kind: 'assistant', text: event.content ?? last.text },
            ]
          }
          return [...current, { kind: 'assistant', text: event.content ?? '' }]
        })
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

  const newConversation = () => {
    abortRef.current?.abort()
    streamingRef.current = false
    sessionIdRef.current = crypto.randomUUID()
    setItems([])
    setDraft('')
    setBusy(false)
  }

  return { items, draft, setDraft, busy, send, stop, newConversation }
}

export type AgentConversation = ReturnType<typeof useAgentConversation>
