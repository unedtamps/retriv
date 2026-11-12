export type PlatformRole = 'admin' | 'user' | string
export type WorkspaceRole = 'owner' | 'editor' | 'viewer'
export type AppRoute = '/dashboard' | '/upload-context'
export type DashboardView =
  | 'chat'
  | 'overview'
  | 'documents'
  | 'members'
  | 'admin-overview'
  | 'api-keys'
  | 'users'
  | 'agent-model'
  | 'help'

export interface SessionToken {
  access_token: string
  token_type: 'bearer'
  expires_in: number
  /** Present on every real response; absent only in older mocks. */
  refresh_token?: string
  refresh_expires_in?: number
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
  member_count: number
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

export interface AdminWorkspace {
  id: string
  slug: string
  name: string
  description: string | null
  created_at: string
  member_count: number
  members: WorkspaceMember[]
}

export interface AdminWorkspaceList {
  items: AdminWorkspace[]
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
  /** Zero unless this is a row-stored file (CSV, XLSX or JSON). */
  row_count: number
}

export interface DocumentList {
  items: DocumentUpload[]
  total: number
  offset: number
  limit: number
}

export interface DocumentSourceRegion {
  page_no: number | null
  bbox: [number, number, number, number] | null
  kind: string
  ref: string
  anchor: string
}

export interface ChunkRegions {
  regions: DocumentSourceRegion[]
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

export interface ProcessingActivity {
  items: ProcessingRun[]
  total: number
}

export interface DocumentSearchHit {
  document_id: string
  workspace_id: string
  workspace_name: string
  filename: string
  media_type: string
  size_bytes: number
  status: string
  row_count: number
  created_at: string
}

export interface DocumentSearchResponse {
  items: DocumentSearchHit[]
  total: number
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

export interface AgentProviderInfo {
  key: string
  label: string
  default_model: string
  key_hint: string
}

export interface AgentModelSettings {
  configured: boolean
  provider: string | null
  base_url: string | null
  model: string | null
  api_key_preview: string | null
  last_test_ok: boolean | null
  last_test_model: string | null
  last_tested_at: string | null
  test_error: string | null
  updated_at: string | null
  key_hint: string | null
  available_providers: AgentProviderInfo[]
}

export interface AgentModelUpdate {
  provider: string
  base_url?: string
  model?: string
  api_key?: string
}

export interface AgentModelTestResult {
  ok: boolean
  model: string | null
  error: string | null
}

export interface RetrievalSettings {
  rerank_enabled: boolean
  rerank_provider: 'laya' | 'bge'
  rerank_model: string
  rerank_blend: number
  rerank_candidates: number
  rerank_concurrency: number
  rerank_min_score: number | null
  rerank_timeout_ms: number
  embedding_base_url: string
  embedding_model: string
  rerank_base_url: string
  last_test_ok: boolean | null
  last_test_ms: number | null
  last_test_error: string | null
  last_tested_at: string | null
  updated_at: string | null
}

export interface RetrievalUpdate {
  rerank_enabled: boolean
  rerank_provider: 'laya' | 'bge'
  rerank_model: string
  rerank_blend: number
  rerank_candidates: number
  rerank_concurrency: number
  rerank_min_score: number | null
  rerank_timeout_ms: number
}

export interface RetrievalTestResult {
  ok: boolean
  model: string | null
  latency_ms: number | null
  score: number | null
  error: string | null
}

export interface AgentChatHistoryMessage {
  role: 'user' | 'assistant'
  content: string
}

/** One source an answer actually cites, labelled with its inline `[n]`. */
export interface AgentChatSource {
  index: number
  document_id: string
  workspace_id?: string
  filename: string | null
  ordinal: number
  section_path?: string[]
  page_numbers?: number[]
}

export interface AgentChatEvent {
  type: 'turn_start' | 'message_delta' | 'tool_call' | 'tool_result' | 'message' | 'final' | 'error'
  sources?: AgentChatSource[]
  name?: string
  arguments?: Record<string, unknown>
  length?: number
  truncated?: boolean
  preview?: string
  content?: string
  message?: string
  retryable?: boolean
  turns_allowed?: number
  replaced?: boolean
  reason?: string
}
