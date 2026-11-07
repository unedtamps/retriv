import { describe, expect, it } from 'vitest'
import type { ChatItem } from '../hooks/useAgentConversation'
import {
  clearChatHistory,
  loadChatHistory,
  saveChatHistory,
  type StoredChatHistory,
} from './chatHistoryStore'

function anItem(text: string): ChatItem {
  return { kind: 'assistant', text, streaming: false }
}

function aHistory(overrides: Partial<StoredChatHistory> = {}): StoredChatHistory {
  return {
    activeId: 'session-1',
    conversations: [
      {
        id: 'session-1',
        title: 'first question',
        items: [{ kind: 'user', text: 'first question' }, anItem('hello')],
        updatedAt: '2026-11-05T10:00:00Z',
      },
    ],
    ...overrides,
  }
}

describe('chat history store', () => {
  it('round-trips an archive of sessions with the active id', () => {
    saveChatHistory(
      aHistory({
        conversations: [
          {
            id: 'session-2',
            title: 'second',
            items: [anItem('second transcript')],
            updatedAt: '2026-11-06T10:00:00Z',
          },
          {
            id: 'session-1',
            title: 'first question',
            items: [anItem('first transcript')],
            updatedAt: '2026-11-05T10:00:00Z',
          },
        ],
      }),
    )
    const restored = loadChatHistory()
    expect(restored.activeId).toBe('session-1')
    expect(restored.conversations.map((entry) => entry.id)).toEqual([
      'session-2',
      'session-1',
    ])
    expect(restored.conversations[0].items[0]).toMatchObject({ text: 'second transcript' })
  })

  it('migrates a v1 single-conversation payload on first load', () => {
    localStorage.setItem(
      'retriv.chat.conversation.v1',
      JSON.stringify({
        sessionId: 'legacy-1',
        items: [{ kind: 'user', text: 'carried over' }],
        savedAt: '2026-11-04T09:00:00Z',
      }),
    )
    const restored = loadChatHistory()
    expect(restored.activeId).toBe('legacy-1')
    expect(restored.conversations).toHaveLength(1)
    expect(restored.conversations[0].id).toBe('legacy-1')
    expect(restored.conversations[0].items[0]).toMatchObject({ text: 'carried over' })
    expect(localStorage.getItem('retriv.chat.conversation.v1')).toBeNull()
  })

  it('strips the streaming flag so a restored item renders as markdown', () => {
    saveChatHistory(
      aHistory({
        conversations: [
          {
            id: 'session-1',
            title: 't',
            items: [{ kind: 'assistant', text: 'partial', streaming: true }],
            updatedAt: '2026-11-05T10:00:00Z',
          },
        ],
      }),
    )
    const restored = loadChatHistory()
    expect(restored.conversations[0].items[0]).not.toHaveProperty('streaming', true)
  })

  it('returns an empty archive for a malformed entry instead of crashing', () => {
    localStorage.setItem('retriv.chat.conversations.v2', '{not json')
    const restored = loadChatHistory()
    expect(restored.activeId).toBeNull()
    expect(restored.conversations).toEqual([])
  })

  it('caps the archive at twenty sessions, keeping the newest', () => {
    const conversations = Array.from({ length: 25 }, (_, index) => ({
      id: `session-${index}`,
      title: `chat ${index}`,
      items: [anItem(`transcript ${index}`)],
      updatedAt: new Date(Date.UTC(2026, 10, index + 1)).toISOString(),
    }))
    saveChatHistory(aHistory({ activeId: 'session-24', conversations }))
    const restored = loadChatHistory()
    expect(restored.conversations).toHaveLength(20)
    expect(restored.conversations[0].id).toBe('session-24')
    expect(
      restored.conversations.some((entry) => entry.id === 'session-0'),
    ).toBe(false)
  })

  it('trims a session whose transcript outgrew the byte cap and still saves', () => {
    const bulky = Array.from({ length: 80 }, (_, index) =>
      anItem(`item ${index} ${'x'.repeat(2_000)}`),
    )
    saveChatHistory(
      aHistory({
        conversations: [
          { id: 'session-big', title: 'big', items: bulky, updatedAt: '2026-11-05T10:00:00Z' },
        ],
      }),
    )
    const restored = loadChatHistory()
    expect(restored.conversations[0].items.length).toBeLessThanOrEqual(60)
    expect(restored.conversations[0].items.at(-1)).toMatchObject({
      text: expect.stringContaining('item 79'),
    })
  })

  it('clear removes every stored session and any legacy payload', () => {
    saveChatHistory(aHistory())
    localStorage.setItem('retriv.chat.conversation.v1', '{"sessionId":"x","items":[]}')
    clearChatHistory()
    const restored = loadChatHistory()
    expect(restored.conversations).toEqual([])
    expect(localStorage.getItem('retriv.chat.conversation.v1')).toBeNull()
  })
})
