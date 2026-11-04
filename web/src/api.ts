import type {
  AdminUserList,
  AdminWorkspaceList,
  AgentChatEvent,
  AgentChatHistoryMessage,
  AgentModelSettings,
  AgentModelTestResult,
  AgentModelUpdate,
  ProcessingActivity,
  ApiKeyList,
  CreatedApiKey,
  CreatedUser,
  CurrentUser,
  DocumentList,
  DocumentSearchResponse,
  DocumentUpload,
  GeneratedPassword,
  MemberCandidateList,
  ProblemDetail,
  SessionToken,
  Workspace,
  WorkspaceList,
  WorkspaceMember,
  WorkspaceMemberList,
  WorkspaceRole,
} from './types'
import { clearSession, readSession, writeSession } from './session'

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim() ?? ''
const API_BASE_URL = configuredBaseUrl.replace(/\/$/, '')

/**
 * How early to renew, as a fraction of the access token's lifetime.
 *
 * Renewing at 80% means a token that lives an hour is replaced with twelve
 * minutes to spare, so a request made just before the deadline still uses a
 * token that is valid on arrival.
 */
const REFRESH_AT_FRACTION = 0.8

export class ApiError extends Error {
  readonly status: number
  readonly code: string | null
  readonly fieldErrors: ProblemDetail['errors']

  constructor(status: number, problem: ProblemDetail | null) {
    super(problem?.detail ?? problem?.title ?? `Request failed with status ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.code = problem?.code ?? null
    this.fieldErrors = problem?.errors ?? []
  }
}

interface RequestOptions extends Omit<RequestInit, 'headers'> {
  headers?: Record<string, string>
  /** Send no Authorization header at all, for the endpoints that mint tokens. */
  anonymous?: boolean
}

interface PageOptions {
  offset?: number
  limit?: number
  signal?: AbortSignal
}

interface UserPageOptions extends PageOptions {
  search?: string
}

/**
 * The two tokens, held for the whole app.
 *
 * They live here rather than in React state because every request needs them
 * and a refresh can happen in the middle of any of them. Threading the token
 * through props meant a component could keep a copy that a refresh had already
 * replaced; with one holder there is nothing to go stale.
 */
let accessToken: string | null = null
let refreshToken: string | null = null
let sessionLostHandler: (() => void) | null = null
let refreshTimer: ReturnType<typeof setTimeout> | null = null
let refreshInFlight: Promise<boolean> | null = null
let accessExpiresAt = 0
let accessLifetimeMs = 0

/** Load whatever session the tab already holds. Call once, before rendering. */
export function restoreSession(): boolean {
  const session = readSession()
  if (session === null) return false
  accessToken = session.accessToken
  refreshToken = session.refreshToken
  return true
}

/** Whether the app currently holds a credential. */
export function hasSession(): boolean {
  return accessToken !== null
}

/**
 * Register what happens when the session cannot be renewed.
 *
 * The app passes a function that returns to the sign-in screen; without one,
 * a dead session simply clears itself and the next request fails as it did
 * before refresh tokens existed.
 */
export function setSessionLostHandler(handler: (() => void) | null): void {
  sessionLostHandler = handler
}

/**
 * Store a freshly minted pair and schedule its renewal.
 *
 * A session with no refresh token (a seeded test session, or a server that
 * predates this feature) is still usable -- it just ends at the first 401.
 */
export function applyTokens(access: string, refresh: string | null, expiresIn?: number): void {
  accessToken = access
  refreshToken = refresh
  // The server's own count, not a decoded claim: the browser may not share the
  // server's clock, and this only has to be good enough to renew early.
  accessLifetimeMs = expiresIn === undefined ? 0 : expiresIn * 1_000
  accessExpiresAt = accessLifetimeMs === 0 ? 0 : Date.now() + accessLifetimeMs
  if (refresh === null) clearSession()
  writeSession({ accessToken: access, refreshToken: refresh })
  scheduleRefresh(expiresIn)
}

/** Drop every credential, cancel the renewal, and forget the tab's copy. */
export function clearTokens(): void {
  accessToken = null
  refreshToken = null
  accessExpiresAt = 0
  accessLifetimeMs = 0
  cancelRefresh()
  clearSession()
}

function scheduleRefresh(expiresIn: number | undefined): void {
  cancelRefresh()
  if (refreshToken === null || expiresIn === undefined || expiresIn <= 0) return
  const delayMs = Math.max(1_000, Math.floor(expiresIn * REFRESH_AT_FRACTION * 1_000))
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    void refreshSession()
  }, delayMs)
}

/**
 * Renew before a request is sent, if the token is already close to expiring.
 *
 * The timer covers the normal case, but a background tab has its timers
 * throttled and a sleeping machine stops them entirely, so the timer alone
 * would let a stale token reach the server. Checking on the way out of every
 * request costs a comparison and makes the timer an optimisation rather than
 * the mechanism.
 */
async function ensureFreshToken(): Promise<void> {
  if (refreshToken === null || accessExpiresAt === 0 || accessLifetimeMs === 0) return
  const remainingMs = accessExpiresAt - Date.now()
  if (remainingMs > accessLifetimeMs * (1 - REFRESH_AT_FRACTION)) return
  await refreshSession()
}

function cancelRefresh(): void {
  if (refreshTimer !== null) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
}

/**
 * Exchange the refresh token for a new pair, at most once at a time.
 *
 * Several requests can be refused within the same second -- a page that loads
 * three resources, say. They must all wait on one exchange, because the server
 * rotates: the second caller would present a token the first already spent,
 * and that looks exactly like a stolen token being replayed.
 *
 * Resolves whether the session is now usable. A failure clears the session,
 * once, and tells the app.
 */
function refreshSession(): Promise<boolean> {
  if (refreshInFlight !== null) return refreshInFlight

  if (refreshToken === null) return Promise.resolve(false)

  const presented = refreshToken
  refreshInFlight = fetch(`${API_BASE_URL}/v1/auth/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ refresh_token: presented }),
    cache: 'no-store',
  })
    .then(async (response) => {
      if (!response.ok) throw new ApiError(response.status, await readProblem(response))
      const body = (await response.json()) as SessionToken
      applyTokens(body.access_token, body.refresh_token ?? null, body.expires_in)
      return true
    })
    .catch(() => {
      clearTokens()
      sessionLostHandler?.()
      return false
    })
    .finally(() => {
      refreshInFlight = null
    })

  return refreshInFlight
}

async function readProblem(response: Response): Promise<ProblemDetail | null> {
  const isJson = response.headers.get('content-type')?.includes('json') ?? false
  if (!isJson) return null
  try {
    return (await response.json()) as ProblemDetail
  } catch {
    return null
  }
}

/**
 * One request, with renewal and a single retry.
 *
 * The retry is safe for every verb the app uses, including the multipart
 * upload: the body is a `FormData` built once and handed to fetch again, not a
 * stream that has already been consumed.
 */
async function send(path: string, options: RequestOptions): Promise<Response> {
  const { anonymous = false, headers: extraHeaders, ...rest } = options
  const build = (token: string | null) => {
    const headers = new Headers(extraHeaders)
    headers.set('Accept', 'application/json')
    if (!anonymous && token !== null) {
      headers.set('Authorization', `Bearer ${token}`)
    }
    return { ...rest, headers, cache: 'no-store' as const }
  }

  if (!anonymous) await ensureFreshToken()
  const presented = accessToken
  let response = await fetch(`${API_BASE_URL}${path}`, build(presented))
  if (response.status !== 401 || anonymous) return response

  // Another request may have renewed the session while this one was in
  // flight. Its token is already replaced, so presenting the old one again
  // would either waste a rotation or -- worse -- look like a replayed token
  // and end the whole session. Retry with what is current; refresh only when
  // nothing has moved.
  if (accessToken !== null && accessToken !== presented) {
    return fetch(`${API_BASE_URL}${path}`, build(accessToken))
  }

  const renewed = await refreshSession()
  if (!renewed) return response
  return fetch(`${API_BASE_URL}${path}`, build(accessToken))
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await send(path, options)

  if (response.status === 204) return undefined as T

  const isJson = response.headers.get('content-type')?.includes('json') ?? false
  const payload = isJson ? ((await response.json()) as unknown) : null
  if (!response.ok) {
    throw new ApiError(response.status, payload as ProblemDetail | null)
  }
  return payload as T
}

function jsonOptions(body: unknown): Pick<RequestOptions, 'body' | 'headers'> {
  return {
    body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' },
  }
}

function adminMemberPath(workspaceId: string, userId?: string): string {
  const base = `/v1/admin/workspaces/${encodeURIComponent(workspaceId)}/members`
  return userId === undefined ? base : `${base}/${encodeURIComponent(userId)}`
}

function ownerMemberPath(workspaceId: string, userId?: string): string {
  const base = `/v1/workspaces/${encodeURIComponent(workspaceId)}/members`
  return userId === undefined ? base : `${base}/${encodeURIComponent(userId)}`
}

export const api = {
  async login(username: string, password: string): Promise<SessionToken> {
    const form = new URLSearchParams({ username, password })
    const session = await request<SessionToken>('/v1/auth/token', {
      method: 'POST',
      body: form,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      anonymous: true,
    })
    applyTokens(session.access_token, session.refresh_token ?? null, session.expires_in)
    return session
  },

  /**
   * End the session on the server, then forget it here.
   *
   * Best-effort on purpose: a network failure must not keep a user signed in
   * on their own screen. The local credential is dropped either way.
   */
  async logout(): Promise<void> {
    const presented = refreshToken
    clearTokens()
    if (presented === null) return
    try {
      await fetch(`${API_BASE_URL}/v1/auth/logout`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ refresh_token: presented }),
        cache: 'no-store',
      })
    } catch {
      return
    }
  },

  me(signal?: AbortSignal): Promise<CurrentUser> {
    return request<CurrentUser>('/v1/auth/me', { signal })
  },

  listWorkspaces(options: PageOptions = {}): Promise<WorkspaceList> {
    const params = new URLSearchParams({
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? 100),
    })
    return request<WorkspaceList>(`/v1/workspaces?${params}`, { signal: options.signal })
  },

  listAdminWorkspaces(options: PageOptions = {}): Promise<AdminWorkspaceList> {
    const params = new URLSearchParams({
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? 100),
    })
    return request<AdminWorkspaceList>(`/v1/admin/workspaces?${params}`, {
      signal: options.signal,
    })
  },

  uploadDocument(workspaceId: string, file: File, signal?: AbortSignal): Promise<DocumentUpload> {
    const form = new FormData()
    form.append('file', file)
    return request<DocumentUpload>(`/v1/workspaces/${encodeURIComponent(workspaceId)}/documents`, {
      method: 'POST',
      body: form,
      signal,
    })
  },

  listDocuments(workspaceId: string, options: PageOptions = {}): Promise<DocumentList> {
    const { offset, limit, signal } = options
    const query = new URLSearchParams()
    if (offset !== undefined) query.set('offset', String(offset))
    if (limit !== undefined) query.set('limit', String(limit))
    const search = query.toString()
    return request<DocumentList>(
      `/v1/workspaces/${encodeURIComponent(workspaceId)}/documents${search === '' ? '' : `?${search}`}`,
      { signal },
    )
  },

  /**
   * Returns 204 as soon as the delete is accepted; the worker removes the
   * vectors and the row afterwards. Documents already being deleted are
   * excluded from listDocuments, so a removed row does not come back.
   */
  deleteDocument(workspaceId: string, documentId: string): Promise<void> {
    return request<void>(
      `/v1/workspaces/${encodeURIComponent(workspaceId)}/documents/${encodeURIComponent(documentId)}`,
      { method: 'DELETE' },
    )
  },

  /**
   * Queue a fresh extraction with the current pipeline. Owner-only: the API
   * answers 403 for an editor and 409 while another run is in flight, and the
   * 202 body carries the document in its new processing state.
   */
  reindexDocument(workspaceId: string, documentId: string): Promise<DocumentUpload> {
    return request<DocumentUpload>(
      `/v1/workspaces/${encodeURIComponent(workspaceId)}/documents/${encodeURIComponent(documentId)}/reindex`,
      { method: 'POST' },
    )
  },

  /**
   * The original file as a Blob, for inline preview.
   *
   * This bypasses `request`: that helper always asks for JSON and throws away
   * a body it cannot parse, while this response is the document itself. The
   * Authorization header is what makes it work from an <iframe> at all -- a
   * browser-driven frame cannot carry one.
   */
  async fetchDocumentBlob(
    workspaceId: string,
    documentId: string,
    signal?: AbortSignal,
  ): Promise<Blob> {
    const path = `/v1/workspaces/${encodeURIComponent(workspaceId)}/documents/${encodeURIComponent(documentId)}/download?disposition=inline`
    const response = await send(path, { headers: { Accept: '*/*' }, signal })
    if (!response.ok) throw new ApiError(response.status, await readProblem(response))
    return response.blob()
  },

  listProcessingActivity(signal?: AbortSignal): Promise<ProcessingActivity> {
    return request<ProcessingActivity>('/v1/processing-runs', { signal })
  },

  searchDocuments(options: {
    query: string
    limit?: number
    signal?: AbortSignal
  }): Promise<DocumentSearchResponse> {
    const params = new URLSearchParams({ q: options.query })
    if (options.limit !== undefined) params.set('limit', String(options.limit))
    return request<DocumentSearchResponse>(`/v1/documents/search?${params}`, {
      signal: options.signal,
    })
  },

  listAdminUsers(options: UserPageOptions = {}): Promise<AdminUserList> {
    const params = new URLSearchParams({
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? 100),
    })
    const search = options.search?.trim() ?? ''
    if (search !== '') params.set('search', search)
    return request<AdminUserList>(`/v1/admin/users?${params}`, { signal: options.signal })
  },

  createUser(username: string, role: 'admin' | 'user'): Promise<CreatedUser> {
    return request<CreatedUser>('/v1/admin/users', {
      method: 'POST',
      ...jsonOptions({ username, role }),
    })
  },

  resetPassword(userId: string): Promise<GeneratedPassword> {
    return request<GeneratedPassword>(
      `/v1/admin/users/${encodeURIComponent(userId)}/password`,
      { method: 'POST' },
    )
  },

  deleteAdminUser(userId: string): Promise<void> {
    return request<void>(`/v1/admin/users/${encodeURIComponent(userId)}`, { method: 'DELETE' })
  },

  createWorkspace(payload: {
    slug: string
    name: string
    description: string | null
    owner_user_id: string
  }): Promise<Workspace> {
    return request<Workspace>('/v1/admin/workspaces', {
      method: 'POST',
      ...jsonOptions(payload),
    })
  },

  listMembers(workspaceId: string, asAdmin: boolean, signal?: AbortSignal) {
    const path = asAdmin ? adminMemberPath(workspaceId) : ownerMemberPath(workspaceId)
    return request<WorkspaceMemberList>(path, { signal })
  },

  searchMemberCandidates(workspaceId: string, search: string): Promise<MemberCandidateList> {
    const params = new URLSearchParams({ search, limit: '20' })
    return request<MemberCandidateList>(
      `/v1/workspaces/${encodeURIComponent(workspaceId)}/member-candidates?${params}`,
      {},
    )
  },

  setMemberRole(
    workspaceId: string,
    userId: string,
    role: WorkspaceRole,
    asAdmin: boolean,
  ): Promise<WorkspaceMember> {
    const path = asAdmin
      ? adminMemberPath(workspaceId, userId)
      : ownerMemberPath(workspaceId, userId)
    return request<WorkspaceMember>(path, {
      method: 'PUT',
      ...jsonOptions({ role }),
    })
  },

  removeMember(workspaceId: string, userId: string, asAdmin: boolean): Promise<void> {
    const path = asAdmin
      ? adminMemberPath(workspaceId, userId)
      : ownerMemberPath(workspaceId, userId)
    return request<void>(path, { method: 'DELETE' })
  },

  listApiKeys(signal?: AbortSignal): Promise<ApiKeyList> {
    return request<ApiKeyList>('/v1/api-keys', { signal })
  },

  createApiKey(name: string): Promise<CreatedApiKey> {
    return request<CreatedApiKey>('/v1/api-keys', {
      method: 'POST',
      ...jsonOptions({ name }),
    })
  },

  revokeApiKey(keyId: string): Promise<void> {
    return request<void>(`/v1/api-keys/${encodeURIComponent(keyId)}`, { method: 'DELETE' })
  },
}

export function getErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error && error.name === 'AbortError') return ''
  return 'The request could not be completed. Check the API connection and try again.'
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}

export async function streamAgentChat(input: {
  message: string
  history: AgentChatHistoryMessage[]
  sessionId: string
  signal: AbortSignal
  onEvent: (event: AgentChatEvent) => void
}): Promise<void> {
  const response = await send('/v1/agent/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message: input.message,
      history: input.history,
      session_id: input.sessionId,
    }),
    signal: input.signal,
  })
  if (!response.ok) {
    const isJson = response.headers.get('content-type')?.includes('json') ?? false
    const payload = isJson ? ((await response.json()) as unknown) : null
    throw new ApiError(response.status, payload as ProblemDetail | null)
  }
  const body = response.body
  if (body === null) throw new ApiError(response.status, null)
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data:')) continue
        const payload = line.slice(5).trim()
        if (!payload) continue
        try {
          input.onEvent(JSON.parse(payload) as AgentChatEvent)
        } catch {
          // A malformed frame is skipped, not fatal: the next one carries on.
        }
      }
      boundary = buffer.indexOf('\n\n')
    }
  }
}

function adminAgentPath(suffix = ''): string {
  return `/v1/admin/agent-model${suffix}`
}

export const agentAdmin = {
  get(signal?: AbortSignal): Promise<AgentModelSettings> {
    return request<AgentModelSettings>(adminAgentPath(), { signal })
  },

  set(update: AgentModelUpdate): Promise<AgentModelSettings> {
    return request<AgentModelSettings>(adminAgentPath(), {
      method: 'PUT',
      ...jsonOptions(update),
    })
  },

  test(update: AgentModelUpdate): Promise<AgentModelTestResult> {
    return request<AgentModelTestResult>(adminAgentPath('/test'), {
      method: 'POST',
      ...jsonOptions(update),
    })
  },
}
