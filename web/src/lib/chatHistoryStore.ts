import type { ChatItem } from '../hooks/useAgentConversation'

/**
 * The conversation archive, kept in this browser and nowhere else.
 *
 * The wire protocol is already stateless -- the client replays the recent
 * history on every turn -- so the server never needed a table, and keeping a
 * whole archive of sessions is a matter of remembering what the client would
 * replay anyway. Each session travels with its own id: providers key routing
 * and prompt caching on it, so returning to an old chat reattaches to the
 * very model session that wrote it.
 *
 * Version 2 holds a list of sessions. Version 1 held the single active
 * conversation; it is migrated in place on first load so an upgrade does not
 * silently drop a transcript.
 */

const STORAGE_KEY = "retriv.chat.conversations.v2"
const LEGACY_KEY = "retriv.chat.conversation.v1"
const MAX_CONVERSATIONS = 20
const KEEP_ITEMS = 60
const MAX_SERIALIZED_BYTES = 1_000_000

export interface StoredConversation {
  id: string
  title: string
  items: ChatItem[]
  updatedAt: string
}

export interface StoredChatHistory {
  activeId: string | null
  conversations: StoredConversation[]
}

export function titleOf(items: ChatItem[]): string {
  const first = items.find((item) => item.kind === "user")
  const line = (first?.text ?? "").split("\n")[0].trim()
  return line === "" ? "New conversation" : line.slice(0, 60)
}

function sanitizeItems(items: ChatItem[]): ChatItem[] {
  return items
    .filter((item) => item !== null && typeof item === "object")
    .slice(-KEEP_ITEMS)
    .map((item) =>
      item.kind === "assistant" && item.streaming === true
        ? { ...item, streaming: undefined }
        : item,
    )
}

export function loadChatHistory(): StoredChatHistory {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw === null) return migrateLegacy()
    const parsed = JSON.parse(raw) as Partial<StoredChatHistory>
    if (!Array.isArray(parsed.conversations)) return migrateLegacy()
    const conversations = parsed.conversations
      .filter(
        (entry): entry is StoredConversation =>
          entry !== null &&
          typeof entry === "object" &&
          typeof entry.id === "string" &&
          Array.isArray(entry.items),
      )
      .map((entry) => ({
        id: entry.id,
        title: typeof entry.title === "string" && entry.title !== "" ? entry.title : titleOf(entry.items),
        items: sanitizeItems(entry.items),
        updatedAt: typeof entry.updatedAt === "string" ? entry.updatedAt : "",
      }))
      .slice(0, MAX_CONVERSATIONS)
    return {
      activeId:
        typeof parsed.activeId === "string" &&
        conversations.some((entry) => entry.id === parsed.activeId)
          ? parsed.activeId
          : null,
      conversations,
    }
  } catch {
    return { activeId: null, conversations: [] }
  }
}

/** A v1 payload was one unnamed conversation; it becomes the first entry. */
function migrateLegacy(): StoredChatHistory {
  try {
    const raw = localStorage.getItem(LEGACY_KEY)
    if (raw === null) return { activeId: null, conversations: [] }
    const parsed = JSON.parse(raw) as {
      sessionId?: unknown
      items?: unknown
      savedAt?: unknown
    }
    if (typeof parsed.sessionId !== "string" || !Array.isArray(parsed.items)) {
      return { activeId: null, conversations: [] }
    }
    const migrated: StoredChatHistory = {
      activeId: parsed.sessionId,
      conversations: [
        {
          id: parsed.sessionId,
          title: titleOf(parsed.items as ChatItem[]),
          items: sanitizeItems(parsed.items as ChatItem[]),
          updatedAt:
            typeof parsed.savedAt === "string" ? parsed.savedAt : new Date().toISOString(),
        },
      ],
    }
    saveChatHistory(migrated)
    localStorage.removeItem(LEGACY_KEY)
    return migrated
  } catch {
    return { activeId: null, conversations: [] }
  }
}

export function saveChatHistory(history: StoredChatHistory): void {
  if (history.conversations.length === 0 && history.activeId === null) {
    localStorage.removeItem(STORAGE_KEY)
    return
  }
  const ordered = [...history.conversations]
    .sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1))
    .slice(0, MAX_CONVERSATIONS)
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...history, conversations: ordered }))
  } catch {
    for (const keep of [10, 5]) {
      try {
        localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify({ ...history, conversations: ordered.slice(0, keep) }),
        )
        return
      } catch {
        continue
      }
    }
    // Out of quota even with five sessions: stop persisting and let the
    // in-memory archive carry on. Nothing is lost but the restore.
  }
}

export function clearChatHistory(): void {
  try {
    localStorage.removeItem(STORAGE_KEY)
    localStorage.removeItem(LEGACY_KEY)
  } catch {
    return
  }
}
