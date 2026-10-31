import { useEffect, useState, type ReactNode } from 'react'
import { CheckCircle2, CircleSlash2, X } from 'lucide-react'
import type {
  AppRoute,
  CurrentUser,
  DashboardView,
  DocumentUpload,
  OneTimeCredential,
  Workspace,
} from '../types'
import { api, getErrorMessage, isUnauthorized } from '../api'
import {
  WORKSPACE_VIEWS,
  dashboardHref,
  readDashboardView,
  readDashboardWorkspaceId,
} from '../lib/dashboardLocation'
import { useProcessingRuns } from '../hooks/useProcessingRuns'
import { useWorkspaceDocuments } from '../hooks/useWorkspaceDocuments'
import { AdminUserPanel } from './AdminUserPanel'
import { AdminWorkspacesPage } from './AdminWorkspaces'
import { ApiKeysPanel } from './ApiKeysPanel'
import { AppShell } from './AppShell'
import { ConfirmDialog, Dialog } from './Dialog'
import { CreateWorkspaceDialog } from './CreateWorkspaceDialog'
import { CredentialDialog } from './CredentialDialog'
import { EmptyWorkspace, WorkspaceOverview } from './WorkspaceOverview'
import { UploadContext } from './UploadContext'
import { UploadForm } from './UploadForm'
import { WorkspaceDocumentsPage } from './WorkspaceDocumentsPage'
import { WorkspaceMembersPage } from './WorkspaceMembersPage'

interface DashboardProps {
  route: AppRoute
  onNavigate: (route: AppRoute) => void
  token: string
  user: CurrentUser
  workspaces: Workspace[]
  onRefreshWorkspaces: () => Promise<void>
  onUnauthorized: () => void
  onLogout: () => void
}

interface Notice {
  message: string
  tone: 'success' | 'error'
}

export function Dashboard({
  route,
  onNavigate,
  token,
  user,
  workspaces,
  onRefreshWorkspaces,
  onUnauthorized,
  onLogout,
}: DashboardProps) {
  const isAdmin = user.role === 'admin'
  const adminMode = isAdmin && workspaces.every((workspace) => workspace.membership_role === null)
  const [activeView, setActiveView] = useState<DashboardView>(
    () =>
      readDashboardView(window.location.search, isAdmin, adminMode) ??
      (adminMode ? 'admin-overview' : 'overview'),
  )
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(() =>
    readDashboardWorkspaceId(window.location.search, workspaces),
  )
  const [searchQuery, setSearchQuery] = useState('')
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
  const documents = useWorkspaceDocuments(token, selectedWorkspace, onUnauthorized)
  const runs = useProcessingRuns(token, onUnauthorized)

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

  function selectWorkspace(workspaceId: string) {
    setSelectedWorkspaceId(workspaceId)
    setSearchQuery('')
    if (route === '/upload-context') return
    if (adminMode) return
    setActiveView((current) => (WORKSPACE_VIEWS.includes(current) ? current : 'overview'))
  }

  function openUpload() {
    if (selectedWorkspace === null || !selectedWorkspace.can_upload) return
    setUploadOpen(true)
  }

  async function deleteDocument() {
    if (deleteTarget === null || selectedWorkspace === null) return
    setDeleting(true)
    try {
      await api.deleteDocument(token, selectedWorkspace.id, deleteTarget.id)
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
      onLogout={onLogout}
    >
      {route === '/upload-context' ? (
        <UploadContext
          token={token}
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
          <ApiKeysPanel token={token} onUnauthorized={onUnauthorized} onNotice={showNotice} />
        </PlatformPage>
      ) : activeView === 'users' && isAdmin ? (
        <PlatformPage
          eyebrow="Platform administration"
          title="Users"
          description="Create accounts, reset passwords and keep the platform directory current."
        >
          <AdminUserPanel
            token={token}
            currentUserId={user.id}
            onCredential={setCredential}
            onUnauthorized={onUnauthorized}
            onNotice={showNotice}
          />
        </PlatformPage>
      ) : adminMode ? (
        <AdminWorkspacesPage
          token={token}
          onUnauthorized={onUnauthorized}
          onNotice={showNotice}
        />
      ) : selectedWorkspace === null ? (
        <EmptyWorkspace onCreate={isAdmin ? () => setCreateWorkspaceOpen(true) : undefined} />
      ) : activeView === 'documents' ? (
        <WorkspaceDocumentsPage
          workspace={selectedWorkspace}
          documents={documents}
          searchQuery={searchQuery}
          onUpload={openUpload}
          onDeleteDocument={setDeleteTarget}
        />
      ) : activeView === 'members' ? (
        <WorkspaceMembersPage
          workspace={selectedWorkspace}
          token={token}
          isAdmin={isAdmin}
          onChanged={onRefreshWorkspaces}
          onUnauthorized={onUnauthorized}
          onNotice={showNotice}
        />
      ) : (
        <WorkspaceOverview
          workspace={selectedWorkspace}
          token={token}
          isAdmin={isAdmin}
          documents={documents}
          searchQuery={searchQuery}
          onUpload={openUpload}
          onViewDocuments={() => setActiveView('documents')}
          onManageMembers={() => setActiveView('members')}
          onUnauthorized={onUnauthorized}
          onDeleteDocument={setDeleteTarget}
        />
      )}

      {createWorkspaceOpen ? (
        <CreateWorkspaceDialog
          open
          token={token}
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
              token={token}
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
        open={deleteTarget !== null}
        title="Delete this document?"
        description={`${deleteTarget?.original_filename ?? 'This document'} and its indexed context will be permanently removed from ${selectedWorkspace?.name ?? 'this workspace'}. This cannot be undone.`}
        confirmLabel="Delete document"
        busy={deleting}
        destructive
        onClose={() => setDeleteTarget(null)}
        onConfirm={deleteDocument}
      />

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
