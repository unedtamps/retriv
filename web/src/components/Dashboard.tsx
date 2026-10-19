import { useDeferredValue, useState } from 'react'
import {
  BookOpenText,
  Building2,
  CheckCircle2,
  ChevronRight,
  CircleSlash2,
  FileUp,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react'
import type { AppRoute, CurrentUser, OneTimeCredential, Workspace } from '../types'
import { AdminUserPanel } from './AdminUserPanel'
import { ApiKeysPanel } from './ApiKeysPanel'
import { AppShell } from './AppShell'
import { CreateWorkspaceDialog } from './CreateWorkspaceDialog'
import { CredentialDialog } from './CredentialDialog'
import { UploadContext } from './UploadContext'
import { DataTable } from './ui/DataTable'
import { EmptyState } from './ui/Feedback'
import { Panel, PanelHeader } from './ui/Panel'
import { SearchField } from './ui/SearchField'
import { WorkspaceDrawer } from './WorkspaceDrawer'

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
  const [query, setQuery] = useState('')
  const deferredQuery = useDeferredValue(query)
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(null)
  const [createWorkspaceOpen, setCreateWorkspaceOpen] = useState(false)
  const [credential, setCredential] = useState<OneTimeCredential | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)

  const isAdmin = user.role === 'admin'
  const normalizedQuery = deferredQuery.trim().toLowerCase()
  const visibleWorkspaces = normalizedQuery === ''
    ? workspaces
    : workspaces.filter((workspace) =>
        `${workspace.name} ${workspace.slug} ${workspace.description ?? ''}`
          .toLowerCase()
          .includes(normalizedQuery),
      )
  const assignedCount = workspaces.filter((workspace) => workspace.membership_role !== null).length
  const ownerCount = workspaces.filter((workspace) => workspace.membership_role === 'owner').length
  const contextCount = workspaces.filter((workspace) => workspace.can_search_context).length
  const selectedWorkspace = workspaces.find((workspace) => workspace.id === selectedWorkspaceId) ?? null

  function showNotice(message: string, tone: 'success' | 'error' = 'success') {
    setNotice({ message, tone })
  }

  return (
    <AppShell
      route={route}
      onNavigate={onNavigate}
      token={token}
      user={user}
      workspaces={workspaces}
      onUnauthorized={onUnauthorized}
      onLogout={onLogout}
    >
      {route === '/upload-context' ? (
        <UploadContext token={token} workspaces={workspaces} onUnauthorized={onUnauthorized} />
      ) : (
        <div className="mx-auto max-w-80rem px-4 py-8 sm:px-6 lg:px-8">
          <header className="flex flex-col gap-4 border-b border-line pb-6 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <p className="eyebrow">Workspace governance</p>
              <h1 className="mt-2 text-2xl font-semibold tracking-[-0.02em] text-ink">
                Access, ownership, and context boundaries.
              </h1>
              <p className="mt-2 max-w-2xl text-sm leading-6 text-ink-muted">
                Every workspace is visible. Its documents and context remain available only to
                assigned members.
              </p>
            </div>
            {isAdmin ? (
              <button
                type="button"
                className="btn btn-primary shrink-0"
                onClick={() => setCreateWorkspaceOpen(true)}
              >
                <Plus aria-hidden="true" size={17} />
                New workspace
              </button>
            ) : null}
          </header>

          {/* The counts that used to be a three-cell strip with 30px numbers.
              They are a summary, not a headline. */}
          <dl className="mt-5 flex flex-wrap items-center gap-x-6 gap-y-1 text-sm text-ink-muted">
            <Count label="visible" value={workspaces.length} />
            <Count label="assigned to you" value={assignedCount} />
            <Count label="owned by you" value={ownerCount} />
            <Count label="with context access" value={contextCount} />
          </dl>

          <Panel className="mt-6">
            <PanelHeader
              id="workspace-catalog-title"
              eyebrow="Directory"
              title="Workspace catalog"
              actions={
                <SearchField
                  id="workspace-search"
                  label="Search workspaces"
                  value={query}
                  onChange={setQuery}
                  placeholder="Search name, slug, or description"
                  className="w-full sm:w-72"
                />
              }
            />
            {visibleWorkspaces.length === 0 ? (
              <EmptyState
                icon={<Search aria-hidden="true" size={20} />}
                title="No matching workspace"
                description="Try a different name, slug, or description."
              />
            ) : (
              <DataTable
                caption="Workspace catalog"
                rows={visibleWorkspaces}
                rowKey={(workspace) => workspace.id}
                columns={[
                  {
                    key: 'workspace',
                    header: 'Workspace',
                    render: (workspace) => (
                      <div className="flex items-start gap-3">
                        <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-card border border-line bg-sunken text-ink-muted">
                          <Building2 aria-hidden="true" size={16} />
                        </span>
                        <div className="min-w-0">
                          <p className="font-medium text-ink">{workspace.name}</p>
                          <p className="mt-0.5 truncate font-mono text-meta text-ink-faint">
                            {workspace.slug}
                          </p>
                        </div>
                      </div>
                    ),
                  },
                  {
                    key: 'role',
                    header: 'Your role',
                    render: (workspace) =>
                      workspace.membership_role === null ? (
                        <span className="text-ink-faint">Unassigned</span>
                      ) : (
                        <span className="badge">{workspace.membership_role}</span>
                      ),
                  },
                  {
                    key: 'capabilities',
                    header: 'Capabilities',
                    render: (workspace) => <CapabilitySummary workspace={workspace} />,
                  },
                  {
                    key: 'details',
                    header: 'Details',
                    align: 'right',
                    render: (workspace) => (
                      <button
                        type="button"
                        className="btn btn-quiet"
                        onClick={() => setSelectedWorkspaceId(workspace.id)}
                      >
                        Open
                        <ChevronRight aria-hidden="true" size={15} />
                      </button>
                    ),
                  },
                ]}
              />
            )}
            <footer className="border-t border-line px-5 py-2.5 font-mono text-meta text-ink-faint">
              {visibleWorkspaces.length} of {workspaces.length} workspaces
            </footer>
          </Panel>

          {isAdmin ? (
            <div id="users" className="mt-6 scroll-mt-20">
              <AdminUserPanel
                token={token}
                onCredential={setCredential}
                onUnauthorized={onUnauthorized}
                onNotice={showNotice}
              />
            </div>
          ) : (
            <section className="mt-6 flex items-start gap-4 rounded-card border border-line bg-surface px-5 py-5">
              <BookOpenText
                aria-hidden="true"
                className="mt-0.5 shrink-0 text-accent-ink"
                size={22}
              />
              <div>
                <h2 className="text-sm font-semibold text-ink">Policy changes are immediate</h2>
                <p className="mt-1 text-sm leading-6 text-ink-muted">
                  Workspace membership is checked by the API on every protected operation, not
                  cached in your login token.
                </p>
              </div>
            </section>
          )}

          <section id="api-keys" className="mt-6 scroll-mt-20">
            <ApiKeysPanel
              token={token}
              onUnauthorized={onUnauthorized}
              onNotice={showNotice}
            />
          </section>
        </div>
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
          }}
        />
      ) : null}
      {selectedWorkspace === null ? null : (
        <WorkspaceDrawer
          key={selectedWorkspace.id}
          workspace={selectedWorkspace}
          token={token}
          isAdmin={isAdmin}
          onChanged={onRefreshWorkspaces}
          onUnauthorized={onUnauthorized}
          onNotice={showNotice}
          onClose={() => setSelectedWorkspaceId(null)}
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

function Count({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="sr-only">{label}</dt>
      <dd className="font-semibold text-ink">{value}</dd>
      <span>{label}</span>
    </div>
  )
}

function CapabilitySummary({ workspace }: { workspace: Workspace }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-muted">
      <span
        className={`inline-flex items-center gap-1 ${workspace.can_upload ? 'text-accent-ink' : 'text-ink-faint'}`}
        title={workspace.can_upload ? 'Upload allowed' : 'Upload unavailable'}
      >
        <FileUp aria-hidden="true" size={14} /> Upload
      </span>
      <span
        className={`inline-flex items-center gap-1 ${workspace.can_search_context ? 'text-accent-ink' : 'text-ink-faint'}`}
        title={workspace.can_search_context ? 'Context allowed' : 'Context unavailable'}
      >
        <Sparkles aria-hidden="true" size={14} /> Context
      </span>
      {workspace.can_manage_members ? (
        <span
          className="inline-flex items-center gap-1 text-accent-ink"
          title="Membership management allowed"
        >
          <ShieldCheck aria-hidden="true" size={14} /> Manage
        </span>
      ) : null}
    </div>
  )
}
