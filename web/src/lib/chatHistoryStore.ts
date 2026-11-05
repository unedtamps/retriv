import type { ChatItem } from '../hooks/useAgentConversation'

/**
 * The conversation, kept in this browser and nowhere else.
 *
 * The wire protocol is already stateless -- the client replays the last
 * twenty messages on every turn -- so the server never needed a table, and
 * persistence is a matter of remembering what the client would replay
 * anyway. The session id travels with the items: providers key routing and
 * prompt caching on it, so a restored transcript without its id would be
 * greeted as a stranger by the very model that wrote it.
 */

const STORAGE_KEY = "retriv.chat.conversation.v1"
const MAX_SERIALIZED_BYTES = 1_000_000
const KEEP_ITEMS = 60

export interface StoredConversation {
  sessionId: string
  items: ChatItem[]
  savedAt: string
}

export function loadChatHistory(): StoredConversation | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw === null) return null
    const parsed = JSON.parse(raw) as Partial<StoredConversation>
    if (
      typeof parsed.sessionId !== "string" ||
      !Array.isArray(parsed.items) ||
      parsed.items.length === 0
    ) {
      return null
    }
    const items = parsed.items
        .filter(
          (item): item is ChatItem =>
            item !== null &&
            typeof item === "object" &&
            typeof (item as ChatItem).text !== "undefined",
        )
        .map((item) =>
          item.kind === "assistant" && item.streaming === true
            ? { ...item, streaming: undefined }
            : item,
        )
    return {
      sessionId: parsed.sessionId,
      items: items.slice(-KEEP_ITEMS),
      savedAt: typeof parsed.savedAt === "string" ? parsed.savedAt : "",
    }
  } catch {
    // A malformed or unreadable entry is the same as no history: the next
    // save overwrites it.
    return null
  }
}

export function saveChatHistory(sessionId: string, items: ChatItem[]): void {
  if (!sessionId || items.length === 0) return
  const payload: StoredConversation = {
    sessionId,
    items: items.map((item) =>
      item.kind === "assistant" && item.streaming === true
        ? { ...item, streaming: undefined }
        : item,
    ),
    savedAt: new Date().toISOString(),
  }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
  } catch {
    const trimmed: StoredConversation = {
      ...payload,
      items: payload.items.slice(-KEEP_ITEMS),
    }
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed))
    } catch {
      // Out of quota with a trimmed payload: stop persisting and let the
      // in-memory conversation carry on. Nothing is lost but the restore.
    }
  }
}

export function clearChatHistory(): void {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    return
  }
}
