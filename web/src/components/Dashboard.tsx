import { useEffect, useState, type ReactNode } from 'react'
import { CheckCircle2, CircleSlash2, X } from 'lucide-react'
import type {
  AppRoute,
  CurrentUser,
  DashboardView,
  DocumentSearchHit,
  DocumentUpload,
  OneTimeCredential,
  Workspace,
} from '../types'
import { api, getErrorMessage, isUnauthorized } from '../api'
import {
  dashboardHref,
  readDashboardView,
  readDashboardWorkspaceId,
} from '../lib/dashboardLocation'
import type { ThemePreference } from '../hooks/useTheme'
import { useProcessingRuns } from '../hooks/useProcessingRuns'
import { useWorkspaceDocuments } from '../hooks/useWorkspaceDocuments'
import { AdminAgentModel } from './AdminAgentModel'
import { AdminUserPanel } from './AdminUserPanel'
import { AdminWorkspacesPage } from './AdminWorkspaces'
import { AgentChatPage } from './AgentChatPage'
import { useAgentConversation } from '../hooks/useAgentConversation'
import { ApiKeysPanel } from './ApiKeysPanel'
import { AppShell } from './AppShell'
import { ConfirmDialog, Dialog } from './Dialog'
import { CreateWorkspaceDialog } from './CreateWorkspaceDialog'
import { DocumentViewerDrawer } from './DocumentViewerDrawer'
import { CredentialDialog } from './CredentialDialog'
import { EmptyWorkspace, WorkspaceOverview } from './WorkspaceOverview'
import { UploadContext } from './UploadContext'
import { UploadForm } from './UploadForm'
import { WorkspaceDocumentsPage } from './WorkspaceDocumentsPage'
import { WorkspaceMembersPage } from './WorkspaceMembersPage'

interface DashboardProps {
  route: AppRoute
  onNavigate: (route: AppRoute) => void
  user: CurrentUser
  workspaces: Workspace[]
  onRefreshWorkspaces: () => Promise<void>
  onUnauthorized: () => void
  onLogout: () => void
  themePreference: ThemePreference
  onThemeChange: (preference: ThemePreference) => void
}

interface Notice {
  message: string
  tone: 'success' | 'error'
}

export function Dashboard({
  route,
  onNavigate,
  user,
  workspaces,
  onRefreshWorkspaces,
  onUnauthorized,
  onLogout,
  themePreference,
  onThemeChange,
}: DashboardProps) {
  const isAdmin = user.role === 'admin'
  const adminMode = isAdmin && workspaces.every((workspace) => workspace.membership_role === null)
  const [activeView, setActiveView] = useState<DashboardView>(
    () =>
      readDashboardView(window.location.search, isAdmin, adminMode) ??
      (adminMode ? 'admin-overview' : 'chat'),
  )
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(() =>
    readDashboardWorkspaceId(window.location.search, workspaces),
  )
  const [searchQuery, setSearchQuery] = useState('')
  const [focusDocumentId, setFocusDocumentId] = useState<string | null>(null)
  const [reindexingId, setReindexingId] = useState<string | null>(null)
  const [reindexTarget, setReindexTarget] = useState<DocumentUpload | null>(null)
  const [viewTarget, setViewTarget] = useState<DocumentUpload | null>(null)
  const [createWorkspaceOpen, setCreateWorkspaceOpen] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<DocumentUpload | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [credential, setCredential] = useState<OneTimeCredential | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)

  const preferredWorkspace =
    workspaces.find((workspace) => workspace.membership_role !== null) ?? workspaces[0] ?? null
  const selectedWorkspace =
    workspaces.find((workspace) => workspace.id === selectedWorkspaceId) ??
    preferredWorkspace
  const documents = useWorkspaceDocuments(selectedWorkspace, onUnauthorized)
  const runs = useProcessingRuns(onUnauthorized)
  const conversation = useAgentConversation(selectedWorkspace?.id ?? '', onUnauthorized)

  useEffect(() => {
    if (route !== '/dashboard') return
    const target = dashboardHref(
      activeView,
      adminMode ? null : selectedWorkspaceId,
      adminMode,
    )
    if (`${window.location.pathname}${window.location.search}` !== target) {
      window.history.replaceState(null, '', target)
    }
  }, [route, activeView, selectedWorkspaceId, adminMode])

  function showNotice(message: string, tone: 'success' | 'error' = 'success') {
    setNotice({ message, tone })
  }

  function selectView(view: DashboardView) {
    if (route !== '/dashboard') onNavigate('/dashboard')
    setActiveView(view)
  }

  /**
   * Change which workspace the console is looking at.
   *
   * The view is left exactly where it is. Documents and Members simply answer
   * for the new workspace, and the platform views (API keys, Users) are not
   * workspace-scoped at all -- switching workspace has no bearing on them, so
   * yanking the user out of API keys would only lose their place.
   */
  function selectWorkspace(workspaceId: string) {
    setSelectedWorkspaceId(workspaceId)
    setSearchQuery('')
  }

  /**
   * Jump to one document from the cross-workspace search.
   *
   * The workspace changes, the view becomes Documents, and the query is set
   * to the filename so the table is guaranteed to contain the row even when
   * it sits past the first page. The id is handed down as a one-shot focus
   * signal the table consumes when it scrolls the row into view.
   */
  function openDocument(hit: DocumentSearchHit) {
    if (route !== '/dashboard') onNavigate('/dashboard')
    setSelectedWorkspaceId(hit.workspace_id)
    setActiveView('documents')
    setSearchQuery(hit.filename)
    setFocusDocumentId(hit.document_id)
    window.setTimeout(() => setFocusDocumentId(null), 2200)
  }

  function openUpload() {
    if (selectedWorkspace === null || !selectedWorkspace.can_upload) return
    setUploadOpen(true)
  }

  async function deleteDocument() {
    if (deleteTarget === null || selectedWorkspace === null) return
    setDeleting(true)
    try {
      await api.deleteDocument(selectedWorkspace.id, deleteTarget.id)
      documents.setData((current) =>
        current === null
          ? current
          : {
              ...current,
              items: current.items.filter((item) => item.id !== deleteTarget.id),
              total: Math.max(0, current.total - 1),
            },
      )
      showNotice(`${deleteTarget.original_filename} deleted from ${selectedWorkspace.name}.`)
      setDeleteTarget(null)
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else showNotice(getErrorMessage(caught), 'error')
    } finally {
      setDeleting(false)
    }
  }

  /**
   * Re-run extraction for one document, on an owner's order.
   *
   * The 202 body already describes the document in its new state, so the row
   * is replaced in place instead of refetching the list: the status stamp
   * turns into "processing" and the pipeline popover picks the run up.
   */
  async function reindexDocument() {
    if (reindexTarget === null || selectedWorkspace === null) return
    setReindexingId(reindexTarget.id)
    try {
      const updated = await api.reindexDocument(selectedWorkspace.id, reindexTarget.id)
      documents.setData((current) =>
        current === null
          ? current
          : {
              ...current,
              items: current.items.map((item) => (item.id === updated.id ? updated : item)),
            },
      )
      showNotice(`${updated.original_filename} queued for reindexing.`)
      setReindexTarget(null)
      runs.reload()
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else showNotice(getErrorMessage(caught), 'error')
    } finally {
      setReindexingId(null)
    }
  }

  return (
    <AppShell
      route={route}
      activeView={activeView}
      onNavigate={onNavigate}
      onSelectView={selectView}
      onSelectWorkspace={selectWorkspace}
      onCreateWorkspace={() => setCreateWorkspaceOpen(true)}
      adminMode={adminMode}
      user={user}
      workspaces={workspaces}
      runs={runs}
      selectedWorkspace={selectedWorkspace}
      searchQuery={searchQuery}
      onSearchQueryChange={setSearchQuery}
      onOpenDocument={openDocument}
      onUnauthorized={onUnauthorized}
      onLogout={onLogout}
      themePreference={themePreference}
      onThemeChange={onThemeChange}
    >
      {route === '/upload-context' ? (
        <UploadContext
          workspaces={workspaces}
          selectedWorkspaceId={selectedWorkspaceId}
          onUnauthorized={onUnauthorized}
        />
      ) : activeView === 'api-keys' ? (
        <PlatformPage
          eyebrow="Programmatic access"
          title="API keys"
          description="Keys authenticate MCP clients with your identity. Workspace access is checked on every call, so membership changes apply immediately."
        >
          <ApiKeysPanel onUnauthorized={onUnauthorized} onNotice={showNotice} />
        </PlatformPage>
      ) : activeView === 'users' && isAdmin ? (
        <PlatformPage
          eyebrow="Platform administration"
          title="Users"
          description="Create accounts, reset passwords and keep the platform directory current."
        >
          <AdminUserPanel
            currentUserId={user.id}
            onCredential={setCredential}
            onUnauthorized={onUnauthorized}
            onNotice={showNotice}
          />
        </PlatformPage>
      ) : activeView === 'agent-model' && isAdmin ? (
        <PlatformPage
          eyebrow="Platform administration"
          title="Agent harness"
          description="The one chat model every workspace agent answers with. Pick a provider, paste its API key, and the connection test proves the endpoint before anyone uses it."
        >
          <AdminAgentModel onUnauthorized={onUnauthorized} onNotice={showNotice} />
        </PlatformPage>
      ) : adminMode ? (
        <AdminWorkspacesPage
          onUnauthorized={onUnauthorized}
          onNotice={showNotice}
        />
      ) : selectedWorkspace === null ? (
        <EmptyWorkspace onCreate={isAdmin ? () => setCreateWorkspaceOpen(true) : undefined} />
      ) : activeView === 'chat' ? (
        <AgentChatPage workspace={selectedWorkspace} conversation={conversation} />
      ) : activeView === 'documents' ? (
        <WorkspaceDocumentsPage
          workspace={selectedWorkspace}
          documents={documents}
          searchQuery={searchQuery}
          focusDocumentId={focusDocumentId}
          canReindex={selectedWorkspace.membership_role === 'owner'}
          reindexingId={reindexingId}
          onReindex={setReindexTarget}
          canView={selectedWorkspace.can_download}
          onView={setViewTarget}
          onUpload={openUpload}
          onDeleteDocument={setDeleteTarget}
        />
      ) : activeView === 'members' ? (
        <WorkspaceMembersPage
          workspace={selectedWorkspace}
          isAdmin={isAdmin}
          onChanged={onRefreshWorkspaces}
          onUnauthorized={onUnauthorized}
          onNotice={showNotice}
        />
      ) : (
        <WorkspaceOverview
          workspace={selectedWorkspace}
          isAdmin={isAdmin}
          documents={documents}
          searchQuery={searchQuery}
          onUpload={openUpload}
          canReindex={selectedWorkspace.membership_role === 'owner'}
          reindexingId={reindexingId}
          onReindex={setReindexTarget}
          canView={selectedWorkspace.can_download}
          onView={setViewTarget}
          onViewDocuments={() => setActiveView('documents')}
          onManageMembers={() => setActiveView('members')}
          onUnauthorized={onUnauthorized}
          onDeleteDocument={setDeleteTarget}
        />
      )}

      {createWorkspaceOpen ? (
        <CreateWorkspaceDialog
          open
          onUnauthorized={onUnauthorized}
          onClose={() => setCreateWorkspaceOpen(false)}
          onCreated={async (workspace) => {
            setCreateWorkspaceOpen(false)
            showNotice(`${workspace.name} created with its initial owner.`)
            await onRefreshWorkspaces()
            setSelectedWorkspaceId(workspace.id)
            setActiveView('overview')
          }}
        />
      ) : null}

      {uploadOpen && selectedWorkspace !== null ? (
        <Dialog
          open
          variant="drawer"
          drawerWidth="narrow"
          title="Upload document"
          eyebrow={`${selectedWorkspace.name} · ${selectedWorkspace.slug}`}
          description="Add a file to this workspace. Extraction and indexing continue in the background."
          onClose={() => setUploadOpen(false)}
        >
          <div className="drawer-scroll px-5 py-5 sm:px-6">
            <UploadForm
              workspaces={workspaces}
              initialWorkspaceId={selectedWorkspace.id}
              lockWorkspace
              onUnauthorized={onUnauthorized}
              onUploaded={() => {
                documents.reload()
                showNotice('Document accepted. Indexing continues in the background.')
              }}
            />
          </div>
        </Dialog>
      ) : null}

      <ConfirmDialog
        open={reindexTarget !== null}
        title="Reindex this document?"
        description={`${reindexTarget?.original_filename ?? 'This document'} will be extracted again with the current pipeline. Its existing chunks, tables and image descriptions are replaced once the new run finishes, and it stays unavailable for search while that happens.`}
        confirmLabel="Reindex document"
        busy={reindexingId !== null}
        onClose={() => setReindexTarget(null)}
        onConfirm={reindexDocument}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete this document?"
        description={`${deleteTarget?.original_filename ?? 'This document'} and its indexed context will be permanently removed from ${selectedWorkspace?.name ?? 'this workspace'}. This cannot be undone.`}
        confirmLabel="Delete document"
        busy={deleting}
        destructive
        onClose={() => setDeleteTarget(null)}
        onConfirm={deleteDocument}
      />

      {viewTarget === null || selectedWorkspace === null ? null : (
        <DocumentViewerDrawer
          document={viewTarget}
          workspace={selectedWorkspace}
          onUnauthorized={onUnauthorized}
          onClose={() => setViewTarget(null)}
        />
      )}

      <CredentialDialog credential={credential} onClose={() => setCredential(null)} />

      {notice === null ? null : (
        <div
          className={notice.tone === 'error' ? 'toast toast-error' : 'toast'}
          role="status"
          aria-live="polite"
        >
          {notice.tone === 'error' ? (
            <CircleSlash2 aria-hidden="true" size={17} />
          ) : (
            <CheckCircle2 aria-hidden="true" size={17} />
          )}
          <span>{notice.message}</span>
          <button
            type="button"
            className="btn btn-quiet ml-auto"
            aria-label="Dismiss notification"
            onClick={() => setNotice(null)}
          >
            <X aria-hidden="true" size={15} />
          </button>
        </div>
      )}
    </AppShell>
  )
}

function PlatformPage({
  eyebrow,
  title,
  description,
  children,
}: {
  eyebrow: string
  title: string
  description: string
  children: ReactNode
}) {
  return (
    <div className="mx-auto max-w-6xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <header className="border-b border-line pb-5">
        <p className="eyebrow">{eyebrow}</p>
        <h1 className="mt-1.5 font-display text-2xl font-medium text-ink">{title}</h1>
        <p className="mt-1.5 max-w-2xl text-sm leading-6 text-ink-muted">{description}</p>
      </header>
      <div className="mt-5">{children}</div>
    </div>
  )
}
