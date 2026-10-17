import type {
  AdminUserList,
  ActiveProcessingRun,
  ApiKeyList,
  CreatedApiKey,
  CreatedUser,
  CurrentUser,
  DocumentList,
  DocumentUpload,
  GeneratedPassword,
  MemberCandidateList,
  ProcessingRunList,
  ProblemDetail,
  SessionToken,
  Workspace,
  WorkspaceList,
  WorkspaceMember,
  WorkspaceMemberList,
  WorkspaceRole,
} from './types'

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim() ?? ''
const API_BASE_URL = configuredBaseUrl.replace(/\/$/, '')

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
  token?: string
  headers?: Record<string, string>
}

interface PageOptions {
  offset?: number
  limit?: number
  signal?: AbortSignal
}

interface UserPageOptions extends PageOptions {
  search?: string
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers)
  headers.set('Accept', 'application/json')
  if (options.token !== undefined) {
    headers.set('Authorization', `Bearer ${options.token}`)
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
    cache: 'no-store',
  })

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
    return request<SessionToken>('/v1/auth/token', {
      method: 'POST',
      body: form,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    })
  },

  me(token: string, signal?: AbortSignal): Promise<CurrentUser> {
    return request<CurrentUser>('/v1/auth/me', { token, signal })
  },

  listWorkspaces(token: string, options: PageOptions = {}): Promise<WorkspaceList> {
    const params = new URLSearchParams({
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? 100),
    })
    return request<WorkspaceList>(`/v1/workspaces?${params}`, {
      token,
      signal: options.signal,
    })
  },

  uploadDocument(
    token: string,
    workspaceId: string,
    file: File,
    signal?: AbortSignal,
  ): Promise<DocumentUpload> {
    const form = new FormData()
    form.append('file', file)
    return request<DocumentUpload>(
      `/v1/workspaces/${encodeURIComponent(workspaceId)}/documents`,
      { token, method: 'POST', body: form, signal },
    )
  },

  listDocuments(
    token: string,
    workspaceId: string,
    options: PageOptions = {},
  ): Promise<DocumentList> {
    const { offset, limit, signal } = options
    const query = new URLSearchParams()
    if (offset !== undefined) query.set('offset', String(offset))
    if (limit !== undefined) query.set('limit', String(limit))
    const search = query.toString()
    return request<DocumentList>(
      `/v1/workspaces/${encodeURIComponent(workspaceId)}/documents${search === '' ? '' : `?${search}`}`,
      { token, signal },
    )
  },

  /**
   * Returns 204 as soon as the delete is accepted; the worker removes the
   * vectors and the row afterwards. Documents already being deleted are
   * excluded from listDocuments, so a removed row does not come back.
   */
  deleteDocument(token: string, workspaceId: string, documentId: string): Promise<void> {
    return request<void>(
      `/v1/workspaces/${encodeURIComponent(workspaceId)}/documents/${encodeURIComponent(documentId)}`,
      { token, method: 'DELETE' },
    )
  },

  getActiveProcessingRun(token: string, signal?: AbortSignal): Promise<ActiveProcessingRun> {
    return request<ActiveProcessingRun>('/v1/processing-runs/active', { token, signal })
  },

  listRecentProcessingRuns(token: string, signal?: AbortSignal): Promise<ProcessingRunList> {
    return request<ProcessingRunList>('/v1/processing-runs/recent', { token, signal })
  },

  listAdminUsers(token: string, options: UserPageOptions = {}): Promise<AdminUserList> {
    const params = new URLSearchParams({
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? 100),
    })
    const search = options.search?.trim() ?? ''
    if (search !== '') params.set('search', search)
    return request<AdminUserList>(`/v1/admin/users?${params}`, { token, signal: options.signal })
  },

  createUser(token: string, username: string, role: 'admin' | 'user'): Promise<CreatedUser> {
    return request<CreatedUser>('/v1/admin/users', {
      token,
      method: 'POST',
      ...jsonOptions({ username, role }),
    })
  },

  resetPassword(token: string, userId: string): Promise<GeneratedPassword> {
    return request<GeneratedPassword>(
      `/v1/admin/users/${encodeURIComponent(userId)}/password`,
      { token, method: 'POST' },
    )
  },

  createWorkspace(
    token: string,
    payload: {
      slug: string
      name: string
      description: string | null
      owner_user_id: string
    },
  ): Promise<Workspace> {
    return request<Workspace>('/v1/admin/workspaces', {
      token,
      method: 'POST',
      ...jsonOptions(payload),
    })
  },

  listMembers(
    token: string,
    workspaceId: string,
    asAdmin: boolean,
    signal?: AbortSignal,
  ): Promise<WorkspaceMemberList> {
    const path = asAdmin ? adminMemberPath(workspaceId) : ownerMemberPath(workspaceId)
    return request<WorkspaceMemberList>(path, { token, signal })
  },

  searchMemberCandidates(
    token: string,
    workspaceId: string,
    search: string,
  ): Promise<MemberCandidateList> {
    const params = new URLSearchParams({ search, limit: '20' })
    return request<MemberCandidateList>(
      `/v1/workspaces/${encodeURIComponent(workspaceId)}/member-candidates?${params}`,
      { token },
    )
  },

  setMemberRole(
    token: string,
    workspaceId: string,
    userId: string,
    role: WorkspaceRole,
    asAdmin: boolean,
  ): Promise<WorkspaceMember> {
    const path = asAdmin
      ? adminMemberPath(workspaceId, userId)
      : ownerMemberPath(workspaceId, userId)
    return request<WorkspaceMember>(path, {
      token,
      method: 'PUT',
      ...jsonOptions({ role }),
    })
  },

  removeMember(
    token: string,
    workspaceId: string,
    userId: string,
    asAdmin: boolean,
  ): Promise<void> {
    const path = asAdmin
      ? adminMemberPath(workspaceId, userId)
      : ownerMemberPath(workspaceId, userId)
    return request<void>(path, { token, method: 'DELETE' })
  },

  listApiKeys(token: string, signal?: AbortSignal): Promise<ApiKeyList> {
    return request<ApiKeyList>('/v1/api-keys', { token, signal })
  },

  createApiKey(token: string, name: string): Promise<CreatedApiKey> {
    return request<CreatedApiKey>('/v1/api-keys', {
      token,
      method: 'POST',
      ...jsonOptions({ name }),
    })
  },

  revokeApiKey(token: string, keyId: string): Promise<void> {
    return request<void>(`/v1/api-keys/${encodeURIComponent(keyId)}`, {
      token,
      method: 'DELETE',
    })
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
