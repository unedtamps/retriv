export type PlatformRole = 'admin' | 'user' | string
export type WorkspaceRole = 'owner' | 'editor' | 'viewer'
export type AppRoute = '/dashboard' | '/upload-context' | '/ask-context'

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

export interface ContextSource {
  workspace_id: string
  document_id: string
  processing_run_id: string
  chunk_id: string
  ordinal: number
  score: number
  content_hash: string
  filename: string | null
  section_path: string[]
  page_numbers: number[]
  text: string
}

export interface ContextAnswer {
  answer: string
  sources: ContextSource[]
}

export interface ConversationSourceReference {
  workspace_id: string
  document_id: string
  processing_run_id: string
  chunk_id: string
  ordinal: number
  score: number
  content_hash: string
  filename: string | null
  section_path: string[]
  page_numbers: number[]
}

export interface AgentConversation {
  id: string
  title: string | null
  created_at: string
  updated_at: string
}

export interface AgentConversationList {
  items: AgentConversation[]
  total: number
  offset: number
  limit: number
}

export interface AgentConversationTurn {
  id: string
  request_id: string
  sequence: number
  question: string
  answer: string | null
  status: 'processing' | 'completed' | 'failed'
  failure_code: string | null
  created_at: string
  completed_at: string | null
  context_available: boolean
  sources: ConversationSourceReference[]
}

export interface AgentConversationTurnList {
  items: AgentConversationTurn[]
  total: number
  offset: number
  limit: number
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
