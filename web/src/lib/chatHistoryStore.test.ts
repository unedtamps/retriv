import { describe, expect, it } from 'vitest'
import type { ChatItem } from '../hooks/useAgentConversation'
import {
  clearChatHistory,
  loadChatHistory,
  saveChatHistory,
} from './chatHistoryStore'

function anItem(text: string): ChatItem {
  return { kind: 'assistant', text, streaming: false }
}

describe('chat history store', () => {
  it('round-trips the transcript and its session id', () => {
    saveChatHistory('session-1', [{ kind: 'user', text: 'hi' }, anItem('hello')])
    const restored = loadChatHistory()
    expect(restored).not.toBeNull()
    expect(restored?.sessionId).toBe('session-1')
    expect(restored?.items.map((item) => item.text)).toEqual(['hi', 'hello'])
  })

  it('strips the streaming flag so a restored item renders as markdown', () => {
    saveChatHistory('session-1', [
      { kind: 'assistant', text: 'partial answer', streaming: true },
    ])
    const restored = loadChatHistory()
    expect(restored?.items[0]).not.toHaveProperty('streaming', true)
  })

  it('returns null for a malformed entry instead of crashing', () => {
    localStorage.setItem('retriv.chat.conversation.v1', '{not json')
    expect(loadChatHistory()).toBeNull()
  })

  it('saves nothing when the conversation is empty', () => {
    saveChatHistory('session-1', [])
    expect(loadChatHistory()).toBeNull()
  })

  it('trims a conversation that outgrew the byte cap and still saves', () => {
    const bulky: ChatItem[] = Array.from({ length: 80 }, (_, index) =>
      anItem(`item ${index} ${'x'.repeat(2_000)}`),
    )
    saveChatHistory('session-1', bulky)
    const restored = loadChatHistory()
    expect(restored).not.toBeNull()
    expect(restored!.items.length).toBeLessThanOrEqual(60)
    expect(restored!.items.at(-1)!.text).toContain('item 79')
  })

  it('clear removes the stored conversation', () => {
    saveChatHistory('session-1', [anItem('hello')])
    clearChatHistory()
    expect(loadChatHistory()).toBeNull()
  })
})
