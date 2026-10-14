export type PlatformRole = 'admin' | 'user' | string
export type WorkspaceRole = 'owner' | 'editor' | 'viewer'
export type AppRoute = '/dashboard' | '/upload-context'

export interface SessionToken {
  access_token: string
  token_type: 'bearer'
  expires_in: number
}

export interface CurrentUser {
  id: string
  username: string
  role: PlatformRole
  is_active: boolean
}

export interface Workspace {
  id: string
  slug: string
  name: string
  description: string | null
  created_at: string
  membership_role: WorkspaceRole | null
  can_upload: boolean
  can_download: boolean
  can_search_context: boolean
  can_manage_members: boolean
  /**
   * Deleting a document is a membership decision only. Do not widen this
   * with an isAdmin check: the API resolves document permissions through
   * the caller's membership and has no platform-admin bypass, so an
   * administrator without one would be offered a button that 404s.
   */
  can_delete: boolean
}

export interface WorkspaceList {
  items: Workspace[]
  total: number
  offset: number
  limit: number
}

export interface DocumentUpload {
  id: string
  workspace_id: string
  original_filename: string
  detected_media_type: string
  size_bytes: number
  sha256: string
  status: string
  /** Null for a record ingested from an external system rather than uploaded. */
  created_by_user_id: string | null
  created_at: string
  duplicate: boolean
  /** Zero unless this is a CSV, whose content is rows rather than chunks. */
  row_count: number
}

export interface DocumentList {
  items: DocumentUpload[]
  total: number
  offset: number
  limit: number
}

export interface ProcessingRun {
  id: string
  workspace_id: string
  document_id: string
  filename: string | null
  job_kind: string
  status: string
  attempts: number
  chunk_count: number
  embedded_chunk_count: number
  row_count: number
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface ActiveProcessingRun {
  item: ProcessingRun | null
}

export interface ProcessingRunList {
  items: ProcessingRun[]
  total: number
  since: string
}

export interface AdminUser {
  id: string
  username: string
  role: PlatformRole
  is_active: boolean
  created_at: string
}

export interface AdminUserList {
  items: AdminUser[]
  total: number
  offset: number
  limit: number
}

export interface CreatedUser {
  id: string
  username: string
  role: PlatformRole
  is_active: boolean
  initial_password: string
}

export interface GeneratedPassword {
  user_id: string
  initial_password: string
}

export interface WorkspaceMember {
  user_id: string
  username: string
  role: WorkspaceRole
  granted_by_user_id: string
  created_at: string
  updated_at: string
}

export interface WorkspaceMemberList {
  items: WorkspaceMember[]
}

export interface MemberCandidate {
  id: string
  username: string
}

export interface MemberCandidateList {
  items: MemberCandidate[]
}

export interface ProblemFieldError {
  field: string
  message: string
}

export interface ProblemDetail {
  title?: string
  detail?: string
  code?: string
  errors?: ProblemFieldError[]
}

export interface OneTimeCredential {
  username: string
  password: string
  reason: 'created' | 'reset'
}

export interface ApiKeySummary {
  id: string
  name: string
  /** Display-only fragment; never enough to reconstruct the key. */
  key_prefix: string
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}

export interface ApiKeyList {
  items: ApiKeySummary[]
}

export interface CreatedApiKey extends ApiKeySummary {
  /** Plaintext credential, present only in the creation response. */
  key: string
  warning: string
}
