export const SESSION_STORAGE_KEY = 'retriv.dashboard.session.v2'
const LEGACY_KEY = 'retriv.dashboard.session.v1'

export interface StoredSession {
  accessToken: string
  refreshToken: string | null
  expiresAt?: number
  lifetimeMs?: number
}

function readRaw(): string | null {
  if (typeof localStorage === 'undefined') return null
  localStorage.removeItem(LEGACY_KEY)
  if (typeof sessionStorage !== 'undefined') {
    sessionStorage.removeItem(LEGACY_KEY)
  }
  const stored = localStorage.getItem(SESSION_STORAGE_KEY)
  if (stored !== null) return stored
  if (typeof sessionStorage === 'undefined') return null
  const carried = sessionStorage.getItem(SESSION_STORAGE_KEY)
  if (carried === null) return null
  localStorage.setItem(SESSION_STORAGE_KEY, carried)
  sessionStorage.removeItem(SESSION_STORAGE_KEY)
  return carried
}

export function readSession(): StoredSession | null {
  const raw = readRaw()
  if (raw === null) return null
  try {
    const stored = JSON.parse(raw) as Partial<StoredSession>
    if (typeof stored.accessToken !== 'string' || stored.accessToken === '') {
      clearSession()
      return null
    }
    const refreshToken =
      typeof stored.refreshToken === 'string' && stored.refreshToken !== ''
        ? stored.refreshToken
        : null
    const expiresAt =
      typeof stored.expiresAt === 'number' && Number.isFinite(stored.expiresAt)
        ? stored.expiresAt
        : undefined
    const lifetimeMs =
      typeof stored.lifetimeMs === 'number' && Number.isFinite(stored.lifetimeMs)
        ? stored.lifetimeMs
        : undefined
    return { accessToken: stored.accessToken, refreshToken, expiresAt, lifetimeMs }
  } catch {
    clearSession()
    return null
  }
}

export function writeSession(session: StoredSession): void {
  if (typeof localStorage === 'undefined') return
  localStorage.setItem(
    SESSION_STORAGE_KEY,
    JSON.stringify({
      accessToken: session.accessToken,
      refreshToken: session.refreshToken,
      expiresAt: session.expiresAt,
      lifetimeMs: session.lifetimeMs,
    }),
  )
  if (typeof sessionStorage !== 'undefined') {
    sessionStorage.removeItem(SESSION_STORAGE_KEY)
    sessionStorage.removeItem(LEGACY_KEY)
  }
}

export function clearSession(): void {
  if (typeof localStorage !== 'undefined') {
    localStorage.removeItem(SESSION_STORAGE_KEY)
    localStorage.removeItem(LEGACY_KEY)
  }
  if (typeof sessionStorage !== 'undefined') {
    sessionStorage.removeItem(SESSION_STORAGE_KEY)
    sessionStorage.removeItem(LEGACY_KEY)
  }
}

export function readSessionToken(): string | null {
  return readSession()?.accessToken ?? null
}

export function writeSessionToken(token: string, refreshToken: string | null = null): void {
  writeSession({ accessToken: token, refreshToken })
}

export function updateSessionTokens(accessToken: string, refreshToken: string): void {
  writeSession({ accessToken, refreshToken })
}
