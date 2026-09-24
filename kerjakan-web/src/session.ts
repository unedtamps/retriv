const SESSION_KEY = 'kerjakan.dashboard.session.v1'

interface StoredSession {
  version: 1
  token: string
}

export function readSessionToken(): string | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY)
    if (raw === null) return null
    const stored = JSON.parse(raw) as Partial<StoredSession>
    return stored.version === 1 && typeof stored.token === 'string'
      ? stored.token
      : null
  } catch {
    sessionStorage.removeItem(SESSION_KEY)
    return null
  }
}

export function writeSessionToken(token: string): void {
  const stored: StoredSession = { version: 1, token }
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(stored))
}

export function clearSessionToken(): void {
  sessionStorage.removeItem(SESSION_KEY)
}
