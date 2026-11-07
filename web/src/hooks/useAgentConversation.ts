import { useEffect, useMemo, useRef, useState } from 'react'
import { getErrorMessage, isUnauthorized, streamAgentChat } from '../api'
import {
  loadChatHistory,
  saveChatHistory,
  titleOf,
  type StoredChatHistory,
  type StoredConversation,
} from '../lib/chatHistoryStore'
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
  | { kind: 'activity'; id?: string; calls: ToolStep[] }
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
 * The conversation archive, owned above the page that shows it.
 *
 * The archive is held here rather than inside the chat page because the page
 * mounts and unmounts as the user moves between views, and a conversation
 * that died on every tab switch would make the tab useless. Every session is
 * also persisted to this browser's storage: the wire protocol replays the
 * recent history on each turn anyway, so what the client remembers is
 * exactly what a continuation needs -- the transcript and the session id the
 * model side keys its caching on. Switching back to an old session restores
 * both, which is what reattaches it to the model session that wrote it.
 *
 * `activeId === null` is the draft state: nothing typed yet, no session in
 * the archive. The first message promotes it into a stored session.
 */
export function useAgentConversation(onUnauthorized: () => void) {
  const [history, setHistory] = useState<StoredChatHistory>(() => loadChatHistory())
  const active = useMemo(
    () => history.conversations.find((entry) => entry.id === history.activeId) ?? null,
    [history],
  )
  const [items, setItems] = useState<ChatItem[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  // One stable id per conversation: providers that key routing or prompt
  // caching on it (OpenCode Go requires it) see the same conversation the
  // whole session lives for.
  const sessionIdRef = useRef<string>(crypto.randomUUID())
  const streamingRef = useRef(false)
  // Bumped on every wholesale transcript replacement (switch, New chat,
  // delete). A pending transcript->archive write captures the epoch when it
  // is scheduled and skips itself if it changed: without this, a switch
  // raced the write and the old conversation resurrected itself into the
  // archive over the empty draft.
  const epochRef = useRef(0)

  // Loading the archive is the mount-time items source; afterwards the
  // session switcher is the only thing that replaces the transcript whole.
  // The transcript's latest copy for callbacks: a session-context change has
  // to flush it synchronously, and the render-scoped `items` would be stale
  // by the time the debounced write was scheduled to run.
  const itemsRef = useRef(items)
  itemsRef.current = items

  const commitTranscript = (promote: boolean) => {
    const transcript = itemsRef.current
    if (transcript.length === 0) return
    const id = sessionIdRef.current
    setHistory((current) => {
      const existing = current.conversations.find((entry) => entry.id === id)
      const updated: StoredConversation = {
        id,
        title: titleOf(transcript),
        items: transcript,
        // The sidebar rises on a sent message, not on this save: send() owns
        // the stamp, and a brand-new session was just messaged into being.
        updatedAt: existing?.updatedAt ?? new Date().toISOString(),
      }
      const conversations = existing
        ? current.conversations.map((entry) => (entry.id === id ? updated : entry))
        : [updated, ...current.conversations]
      // A debounced write promotes the draft into the active session; a
      // flush before a switch must not yank the active id -- the switch
      // itself decides what becomes active next.
      return promote || current.activeId === id
        ? { activeId: id, conversations }
        : { ...current, conversations }
    })
  }

  const loadDraft = () => {
    sessionIdRef.current = crypto.randomUUID()
    streamingRef.current = false
    setItems([])
    setBusy(false)
  }

  useEffect(() => {
    // A draft's first message promotes itself: the id that just became active
    // is the session already on screen. That is not a switch -- treating it
    // as one would abort the request this hook itself just started.
    if (history.activeId !== null && history.activeId === sessionIdRef.current) {
      return
    }
    abortRef.current?.abort()
    epochRef.current += 1
    streamingRef.current = false
    setBusy(false)
    if (active === null) {
      loadDraft()
      return
    }
    sessionIdRef.current = active.id
    setItems(active.items)
    // `history` changes identity on every save; only an actual switch of the
    // active session may reload the transcript.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history.activeId])

  // Local transcript -> archive, debounced. The first message of a draft
  // promotes it into a stored session and makes it the active one.
  useEffect(() => {
    const timer = setTimeout(() => commitTranscript(true), 300)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items])

  // Archive -> this browser. The upsert above is already the throttle, so
  // every archive change is written straight through.
  useEffect(() => {
    saveChatHistory(history)
  }, [history])

  useEffect(() => () => abortRef.current?.abort(), [])

  const send = async () => {
    const message = draft.trim()
    if (message === '' || busy) return
    setDraft('')
    setBusy(true)
    const withQuestion: ChatItem[] = [...items, { kind: 'user', text: message }]
    // A stable id, not object identity: the first patch replaces the item,
    // and an identity check would then miss every later tool call.
    const activityId = crypto.randomUUID()
    const activity: ChatItem = { kind: 'activity', id: activityId, calls: [] }
    setItems([...withQuestion, activity])
    // The one moment the session's sidebar position may change: a message
    // was sent. Saves and switches leave the order alone.
    setHistory((current) => ({
      ...current,
      conversations: current.conversations.map((entry) =>
        entry.id === sessionIdRef.current
          ? { ...entry, updatedAt: new Date().toISOString() }
          : entry,
      ),
    }))
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
          item.kind === 'activity' && item.id === activityId
            ? { kind: 'activity', id: activityId, calls: patch(item.calls) }
            : item,
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
    commitTranscript(false)
    // Cleared directly rather than through the activeId transition: from the
    // draft state activeId is already null, and a no-op set would leave the
    // draft's transcript on screen. The archive keeps the old session -- the
    // sidebar still lists it, and its id is what a click restores.
    epochRef.current += 1
    loadDraft()
    setDraft('')
  }

  const selectSession = (id: string) => {
    if (id === history.activeId) return
    commitTranscript(false)
    epochRef.current += 1
    setHistory((current) => ({ ...current, activeId: id }))
  }

  const deleteSession = (id: string) => {
    abortRef.current?.abort()
    commitTranscript(false)
    const wasActive = history.activeId === id
    epochRef.current += 1
    setHistory((current) => ({
      activeId: current.activeId === id ? null : current.activeId,
      conversations: current.conversations.filter((entry) => entry.id !== id),
    }))
    // Deleting the active session of a draft lands on activeId null -> null:
    // the sync effect would not re-run, so clear the transcript here.
    if (wasActive) loadDraft()
  }

  return {
    items,
    draft,
    setDraft,
    busy,
    send,
    stop,
    newConversation,
    sessions: history.conversations,
    activeId: history.activeId,
    selectSession,
    deleteSession,
  }
}

export type AgentConversation = ReturnType<typeof useAgentConversation>
