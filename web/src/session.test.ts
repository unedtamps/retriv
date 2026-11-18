import { describe, expect, it } from 'vitest'
import {
  SESSION_STORAGE_KEY,
  clearSession,
  readSession,
  writeSession,
} from './session'

const session = {
  accessToken: 'access-token',
  refreshToken: 'refresh-token',
  expiresAt: 1_700_000_000_000,
  lifetimeMs: 3_600_000,
}

describe('session storage', () => {
  it('keeps the session in localStorage', () => {
    writeSession(session)

    expect(JSON.parse(localStorage.getItem(SESSION_STORAGE_KEY) ?? 'null')).toEqual(session)
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull()
    expect(readSession()).toEqual(session)
  })

  it('migrates a tab-scoped session into localStorage', () => {
    sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session))

    expect(readSession()).toEqual(session)
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull()
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).not.toBeNull()
  })

  it('clears both stores', () => {
    localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session))
    sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session))

    clearSession()

    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull()
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull()
  })
})
