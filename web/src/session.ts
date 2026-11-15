const SESSION_KEY = 'retriv.dashboard.session.v2'
const LEGACY_KEY = 'retriv.dashboard.session.v1'

export interface StoredSession {
  accessToken: string
  refreshToken: string | null
}

export function readSession(): StoredSession | null {
  if (typeof sessionStorage === 'undefined') return null
  sessionStorage.removeItem(LEGACY_KEY)
  try {
    const raw = sessionStorage.getItem(SESSION_KEY)
    if (raw === null) return null
    const stored = JSON.parse(raw) as Partial<StoredSession>
    if (typeof stored.accessToken !== 'string' || stored.accessToken === '') return null
    const refreshToken =
      typeof stored.refreshToken === 'string' && stored.refreshToken !== ''
        ? stored.refreshToken
        : null
    return { accessToken: stored.accessToken, refreshToken }
  } catch {
    sessionStorage.removeItem(SESSION_KEY)
    return null
  }
}

export function writeSession(session: StoredSession): void {
  sessionStorage.setItem(
    SESSION_KEY,
    JSON.stringify({ accessToken: session.accessToken, refreshToken: session.refreshToken }),
  )
}

export function clearSession(): void {
  sessionStorage.removeItem(SESSION_KEY)
  sessionStorage.removeItem(LEGACY_KEY)
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
