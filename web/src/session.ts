const SESSION_KEY = 'retriv.dashboard.session.v2'
const LEGACY_KEY = 'retriv.dashboard.session.v1'

/**
 * The session: a short-lived access token and the long-lived token that
 * renews it.
 *
 * Both live in `sessionStorage`, so a session ends when the tab does. That is
 * the promise the sign-in screen makes, and it keeps a 30-day credential out
 * of `localStorage`, where any script on the origin can read it and where it
 * survives the browser closing.
 *
 * A session written by an older build (v1: access token only) is treated as
 * absent. It cannot be refreshed, so honouring it would only postpone the
 * login by one access-token lifetime.
 */
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

/** The access token alone, or null when there is no session. */
export function readSessionToken(): string | null {
  return readSession()?.accessToken ?? null
}

/**
 * Store an access-only session.
 *
 * The refresh token is optional at this layer because a session can exist
 * without one -- a test that seeds a token directly, or a deployment whose
 * refresh endpoint is unreachable. Such a session behaves exactly as before:
 * it simply ends at the next 401.
 */
export function writeSessionToken(token: string, refreshToken: string | null = null): void {
  writeSession({ accessToken: token, refreshToken })
}

/** Replace the access token after a refresh, keeping the refresh token. */
export function updateSessionTokens(accessToken: string, refreshToken: string): void {
  writeSession({ accessToken, refreshToken })
}
