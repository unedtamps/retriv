import { useDeferredValue, useEffect, useRef, useState, type MouseEvent } from 'react'
import {
  ArrowUpRight,
  BookOpenText,
  Building2,
  CheckCircle2,
  ChevronRight,
  CircleSlash2,
  FileUp,
  KeyRound,
  LayoutGrid,
  LogOut,
  Menu,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  Users,
  X,
} from 'lucide-react'
import type { AppRoute, CurrentUser, OneTimeCredential, Workspace } from '../types'
import { AdminUserPanel } from './AdminUserPanel'
import { ApiKeysPanel } from './ApiKeysPanel'
import { Brand } from './LoginScreen'
import { CreateWorkspaceDialog } from './CreateWorkspaceDialog'
import { CredentialDialog } from './CredentialDialog'
import { ProcessingNotifications } from './ProcessingNotifications'
import { UploadContext } from './UploadContext'
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
  const [navigationOpen, setNavigationOpen] = useState(false)
  const [notificationsOpen, setNotificationsOpen] = useState(false)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const drawerCloseButtonRef = useRef<HTMLButtonElement>(null)

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

  function openUpload(event: MouseEvent<HTMLAnchorElement>) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    setNavigationOpen(false)
    setSelectedWorkspaceId(null)
    onNavigate('/upload-context')
  }

  function navigateFromDrawer(event: MouseEvent<HTMLAnchorElement>, destination: AppRoute) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    setNavigationOpen(false)
    setSelectedWorkspaceId(null)
    onNavigate(destination)
  }

  function closeNavigation() {
    setNavigationOpen(false)
  }

  useEffect(() => {
    if (!navigationOpen) return

    const previousOverflow = document.body.style.overflow
    const menuButton = menuButtonRef.current
    document.body.style.overflow = 'hidden'
    drawerCloseButtonRef.current?.focus()

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') setNavigationOpen(false)
    }

    window.addEventListener('keydown', closeOnEscape)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', closeOnEscape)
      window.requestAnimationFrame(() => menuButton?.focus())
    }
  }, [navigationOpen])

  return (
    <div className="min-h-svh bg-[#f4f1e8] text-stone-800">
      <a href="#dashboard-content" className="skip-link">Skip to dashboard content</a>
      <header inert={navigationOpen || notificationsOpen} className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-stone-300 bg-[#f4f1e8]/95 px-4 backdrop-blur sm:px-6">
        <button
          ref={menuButtonRef}
          type="button"
          className="icon-button"
          aria-label={navigationOpen ? 'Navigation open' : 'Open navigation'}
          aria-controls="primary-navigation"
          aria-expanded={navigationOpen}
          onClick={() => setNavigationOpen(true)}
        >
          <Menu aria-hidden="true" size={19} />
        </button>
        <Brand />
        <span className="ml-auto hidden font-mono text-[0.65rem] uppercase tracking-[0.14em] text-stone-500 sm:block">
          {route === '/upload-context' ? 'Document ingestion' : 'Workspace governance'}
        </span>
        <ProcessingNotifications
          token={token}
          workspaces={workspaces}
          onUnauthorized={onUnauthorized}
          onOpenChange={setNotificationsOpen}
        />
      </header>

      {navigationOpen ? (
        <button
          type="button"
          className="fixed inset-0 z-40 cursor-pointer bg-[#17231f]/45 backdrop-blur-[1px]"
          aria-label="Close navigation"
          onClick={closeNavigation}
        />
      ) : null}

      <aside
        id="primary-navigation"
        role="dialog"
        aria-label="Primary navigation"
        aria-modal={navigationOpen}
        aria-hidden={!navigationOpen}
        inert={!navigationOpen}
        className={`fixed inset-y-0 left-0 z-50 flex w-72 max-w-[85vw] flex-col border-r border-stone-800 bg-[#17231f] px-5 py-6 text-stone-100 shadow-[0_24px_80px_rgba(0,0,0,0.3)] transition-transform duration-200 motion-reduce:transition-none ${navigationOpen ? 'translate-x-0' : '-translate-x-full'}`}
      >
        <div className="flex items-center justify-between gap-3">
          <Brand light />
          <button
            ref={drawerCloseButtonRef}
            type="button"
            className="icon-button border-white/20 text-white hover:bg-white/10"
            aria-label="Close navigation drawer"
            onClick={closeNavigation}
          >
            <X aria-hidden="true" size={19} />
          </button>
        </div>
        <nav className="mt-10 space-y-2" aria-label="Main navigation">
          <a
            className={route === '/dashboard' ? 'nav-item nav-item-active' : 'nav-item'}
            href="/dashboard"
            aria-current={route === '/dashboard' ? 'page' : undefined}
            onClick={(event) => navigateFromDrawer(event, '/dashboard')}
          >
            <LayoutGrid aria-hidden="true" size={17} />
            Dashboard
          </a>
          <a
            className="nav-item"
            href={route === '/dashboard' ? '#api-keys' : '/dashboard#api-keys'}
            onClick={closeNavigation}
          >
            <KeyRound aria-hidden="true" size={17} />
            API keys
          </a>
          <a
            className={route === '/upload-context' ? 'nav-item nav-item-active' : 'nav-item'}
            href="/upload-context"
            aria-current={route === '/upload-context' ? 'page' : undefined}
            onClick={openUpload}
          >
            <FileUp aria-hidden="true" size={17} />
            Upload context
          </a>
          {isAdmin ? (
            <a
              className="nav-item"
              href={route === '/dashboard' ? '#users' : '/dashboard#users'}
              onClick={closeNavigation}
            >
              <Users aria-hidden="true" size={17} />
              Users
            </a>
          ) : null}
        </nav>
        <div className="mt-auto border-t border-white/10 pt-5">
          <p className="font-mono text-[0.62rem] uppercase tracking-[0.17em] text-stone-500">Signed in as</p>
          <div className="mt-3 flex items-center gap-3">
            <span className="flex h-9 w-9 items-center justify-center border border-white/15 bg-white/5 text-sm font-semibold uppercase text-[#d9f15f]">
              {user.username.slice(0, 2)}
            </span>
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-white">{user.username}</p>
              <p className="mt-0.5 text-xs capitalize text-stone-400">{user.role}</p>
            </div>
          </div>
          <button type="button" className="mt-5 flex w-full cursor-pointer items-center gap-2 text-sm text-stone-400 transition-colors hover:text-white focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#d9f15f]" onClick={onLogout}>
            <LogOut aria-hidden="true" size={16} />
            Sign out
          </button>
        </div>
      </aside>

      <main id="dashboard-content" inert={navigationOpen || notificationsOpen}>
        {route === '/upload-context' ? (
          <UploadContext token={token} workspaces={workspaces} onUnauthorized={onUnauthorized} />
        ) : (
        <div className="mx-auto max-w-[96rem] px-4 py-8 sm:px-6 lg:px-10 lg:py-10 xl:px-12">
          <header className="flex flex-col gap-6 border-b border-stone-300 pb-8 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <p className="section-eyebrow">Workspace governance</p>
              <h1 className="mt-3 max-w-3xl text-3xl font-semibold tracking-[-0.04em] text-stone-950 sm:text-4xl">
                Access, ownership, and context boundaries.
              </h1>
              <p className="mt-3 max-w-2xl text-sm leading-6 text-stone-600 sm:text-base">
                Every workspace is visible. Its documents and context remain available only to assigned members.
              </p>
            </div>
            {isAdmin ? (
              <button type="button" className="button-primary shrink-0" onClick={() => setCreateWorkspaceOpen(true)}>
                <Plus aria-hidden="true" size={17} />
                New workspace
              </button>
            ) : null}
          </header>

          <section className="grid border-x border-b border-stone-300 bg-white sm:grid-cols-3" aria-label="Workspace summary">
            <Metric label="Workspace catalog" value={workspaces.length} detail="Visible across the platform" />
            <Metric label="Assigned to you" value={assignedCount} detail={`${ownerCount} owned workspace${ownerCount === 1 ? '' : 's'}`} />
            <Metric label="Context enabled" value={contextCount} detail="Authorized for retrieval" />
          </section>

          <section id="workspaces" className="mt-8 scroll-mt-24 border border-stone-300 bg-white" aria-labelledby="workspace-catalog-title">
            <header className="flex flex-col gap-4 border-b border-stone-200 px-5 py-5 sm:flex-row sm:items-center sm:justify-between lg:px-6">
              <div>
                <p className="section-eyebrow">Directory</p>
                <h2 id="workspace-catalog-title" className="mt-1 text-xl font-semibold tracking-[-0.02em] text-stone-950">Workspace catalog</h2>
              </div>
              <div className="relative w-full sm:w-80">
                <Search className="field-icon" aria-hidden="true" size={16} />
                <label className="sr-only" htmlFor="workspace-search">Search workspaces</label>
                <input
                  id="workspace-search"
                  className="field-input h-10 pl-10"
                  value={query}
                  placeholder="Search name, slug, or description"
                  onChange={(event) => setQuery(event.target.value)}
                />
              </div>
            </header>

            {visibleWorkspaces.length === 0 ? (
              <div className="px-5 py-16 text-center">
                <Search className="mx-auto text-stone-300" aria-hidden="true" size={28} />
                <h3 className="mt-4 font-semibold text-stone-950">No matching workspace</h3>
                <p className="mt-2 text-sm text-stone-500">Try a different name, slug, or description.</p>
              </div>
            ) : (
              <>
                <div className="hidden overflow-x-auto sm:block">
                  <table className="data-table min-w-[50rem]">
                    <thead><tr><th>Workspace</th><th>Your role</th><th>Capabilities</th><th className="text-right">Details</th></tr></thead>
                    <tbody>
                      {visibleWorkspaces.map((workspace) => (
                        <tr key={workspace.id}>
                          <td>
                            <div className="flex items-start gap-3">
                              <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center border border-stone-200 bg-stone-50 text-emerald-900"><Building2 aria-hidden="true" size={17} /></span>
                              <div className="min-w-0">
                                <p className="font-medium text-stone-950">{workspace.name}</p>
                                <p className="mt-1 max-w-md truncate text-xs text-stone-500">{workspace.description ?? workspace.slug}</p>
                              </div>
                            </div>
                          </td>
                          <td>{workspace.membership_role === null ? <span className="text-sm text-stone-400">Unassigned</span> : <span className="role-badge">{workspace.membership_role}</span>}</td>
                          <td><CapabilitySummary workspace={workspace} /></td>
                          <td className="text-right"><button type="button" className="text-button ml-auto" onClick={() => setSelectedWorkspaceId(workspace.id)}>Open <ChevronRight aria-hidden="true" size={15} /></button></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="divide-y divide-stone-200 sm:hidden">
                  {visibleWorkspaces.map((workspace) => (
                    <button key={workspace.id} type="button" className="block w-full cursor-pointer px-5 py-5 text-left transition-colors hover:bg-stone-50 focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-emerald-800" onClick={() => setSelectedWorkspaceId(workspace.id)}>
                      <div className="flex items-start justify-between gap-4"><div><p className="font-medium text-stone-950">{workspace.name}</p><p className="mt-1 font-mono text-[0.65rem] text-stone-400">{workspace.slug}</p></div><ArrowUpRight aria-hidden="true" className="text-stone-400" size={17} /></div>
                      <div className="mt-4 flex items-center justify-between gap-4">{workspace.membership_role === null ? <span className="text-xs text-stone-400">Catalog only</span> : <span className="role-badge">{workspace.membership_role}</span>}<CapabilitySummary workspace={workspace} /></div>
                    </button>
                  ))}
                </div>
              </>
            )}
            <footer className="border-t border-stone-200 px-5 py-3 font-mono text-[0.65rem] uppercase tracking-[0.12em] text-stone-500 lg:px-6">{visibleWorkspaces.length} of {workspaces.length} workspaces</footer>
          </section>

          {isAdmin ? (
            <div id="users" className="mt-8 scroll-mt-24">
              <AdminUserPanel token={token} onCredential={setCredential} onUnauthorized={onUnauthorized} onNotice={showNotice} />
            </div>
          ) : (
            <section className="mt-8 grid gap-4 border border-stone-300 bg-[#17231f] p-6 text-white sm:grid-cols-[auto_1fr] sm:items-center sm:p-8">
              <BookOpenText aria-hidden="true" className="text-[#d9f15f]" size={28} strokeWidth={1.6} />
              <div><h2 className="text-lg font-semibold">Policy changes are immediate</h2><p className="mt-1 text-sm leading-6 text-stone-300">Workspace membership is checked by the API on every protected operation, not cached in your login token.</p></div>
            </section>
          )}

          <section id="api-keys" className="mt-8 scroll-mt-24">
            <ApiKeysPanel token={token} onUnauthorized={onUnauthorized} onNotice={showNotice} />
          </section>
        </div>
        )}
      </main>

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
        <WorkspaceDrawer key={selectedWorkspace.id} workspace={selectedWorkspace} token={token} isAdmin={isAdmin} onChanged={onRefreshWorkspaces} onUnauthorized={onUnauthorized} onNotice={showNotice} onClose={() => setSelectedWorkspaceId(null)} />
      )}
      <CredentialDialog credential={credential} onClose={() => setCredential(null)} />
      {notice === null ? null : (
        <div className={notice.tone === 'error' ? 'notice notice-error' : 'notice'} role="status" aria-live="polite">
          {notice.tone === 'error' ? <CircleSlash2 aria-hidden="true" size={17} /> : <CheckCircle2 aria-hidden="true" size={17} />}
          <span>{notice.message}</span>
          <button type="button" aria-label="Dismiss notification" onClick={() => setNotice(null)}><X aria-hidden="true" size={15} /></button>
        </div>
      )}
    </div>
  )
}

interface MetricProps { label: string; value: number; detail: string }

function Metric({ label, value, detail }: MetricProps) {
  return <div className="border-b border-stone-200 px-5 py-5 last:border-b-0 sm:border-b-0 sm:border-r sm:last:border-r-0 lg:px-6"><p className="font-mono text-[0.65rem] uppercase tracking-[0.14em] text-stone-500">{label}</p><div className="mt-2 flex items-end gap-3"><p className="text-3xl font-semibold tracking-[-0.04em] text-stone-950">{value}</p><p className="pb-1 text-xs text-stone-500">{detail}</p></div></div>
}

function CapabilitySummary({ workspace }: { workspace: Workspace }) {
  return (
    <div className="flex items-center gap-3 text-xs text-stone-500">
      <span className={workspace.can_upload ? 'capability-on' : 'capability-off'} title={workspace.can_upload ? 'Upload allowed' : 'Upload unavailable'}><FileUp aria-hidden="true" size={14} />Upload</span>
      <span className={workspace.can_search_context ? 'capability-on' : 'capability-off'} title={workspace.can_search_context ? 'Context allowed' : 'Context unavailable'}><Sparkles aria-hidden="true" size={14} />Context</span>
      {workspace.can_manage_members ? <span className="capability-on" title="Membership management allowed"><ShieldCheck aria-hidden="true" size={14} />Manage</span> : null}
    </div>
  )
}
