import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import {
  BookOpenText,
  FileText,
  FileUp,
  KeyRound,
  LayoutGrid,
  LogOut,
  Menu,
  Plus,
  Search,
  Users,
  X,
} from 'lucide-react'
import type { AppRoute, CurrentUser, DashboardView, Workspace } from '../types'
import { ProcessingNotifications } from './ProcessingNotifications'
import { Avatar } from './ui/Avatar'
import { BrandLockup, BrandMark } from './ui/Brand'
import { useIsDesktop } from '../hooks/useIsDesktop'

interface AppShellProps {
  route: AppRoute
  activeView: DashboardView
  onNavigate: (route: AppRoute) => void
  onSelectView: (view: DashboardView) => void
  onSelectWorkspace: (workspaceId: string) => void
  onCreateWorkspace: () => void
  token: string
  user: CurrentUser
  workspaces: Workspace[]
  selectedWorkspace: Workspace | null
  searchQuery: string
  onSearchQueryChange: (query: string) => void
  onUnauthorized: () => void
  onLogout: () => void
  children: ReactNode
}

const WORKSPACE_NAV: Array<{ view: DashboardView; label: string; icon: typeof LayoutGrid }> = [
  { view: 'overview', label: 'Overview', icon: LayoutGrid },
  { view: 'documents', label: 'Documents', icon: FileText },
  { view: 'members', label: 'Members', icon: Users },
]

/**
 * The frame around every page: a rail on a wide screen, a drawer on a narrow
 * one, and a bar across the top.
 */
export function AppShell({
  route,
  activeView,
  onNavigate,
  onSelectView,
  onSelectWorkspace,
  onCreateWorkspace,
  token,
  user,
  workspaces,
  selectedWorkspace,
  searchQuery,
  onSearchQueryChange,
  onUnauthorized,
  onLogout,
  children,
}: AppShellProps) {
  const [navigationOpen, setNavigationOpen] = useState(false)
  const [notificationsOpen, setNotificationsOpen] = useState(false)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const drawerCloseButtonRef = useRef<HTMLButtonElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const isDesktop = useIsDesktop()
  const isAdmin = user.role === 'admin'
  const showSearch =
    route === '/dashboard' && activeView !== 'api-keys' && activeView !== 'users'

  useEffect(() => {
    if (!navigationOpen || isDesktop) return

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
  }, [navigationOpen, isDesktop])

  useEffect(() => {
    function focusSearch(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        searchRef.current?.focus()
      }
    }
    window.addEventListener('keydown', focusSearch)
    return () => window.removeEventListener('keydown', focusSearch)
  }, [])

  function navigateFromNav(event: MouseEvent<HTMLAnchorElement>, destination: AppRoute) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    setNavigationOpen(false)
    onNavigate(destination)
  }

  function selectView(view: DashboardView) {
    setNavigationOpen(false)
    onSelectView(view)
  }

  function selectWorkspace(workspaceId: string) {
    setNavigationOpen(false)
    onSelectWorkspace(workspaceId)
  }

  return (
    <div className="min-h-svh bg-paper text-ink">
      <a href="#main-content" className="skip-link">
        Skip to content
      </a>

      {!isDesktop && navigationOpen ? (
        <button
          type="button"
          className="fixed inset-0 z-40 cursor-pointer bg-ink/40 backdrop-blur-[1px]"
          aria-label="Close navigation"
          onClick={() => setNavigationOpen(false)}
        />
      ) : null}

      <div
        id="primary-navigation"
        role={isDesktop ? undefined : 'dialog'}
        aria-label={isDesktop ? undefined : 'Primary navigation'}
        aria-modal={isDesktop ? undefined : true}
        aria-hidden={isDesktop ? undefined : !navigationOpen}
        inert={isDesktop ? undefined : !navigationOpen}
        className={
          isDesktop
            ? 'fixed inset-y-0 left-0 z-30 flex w-[15.5rem] flex-col border-r border-line bg-surface'
            : `fixed inset-y-0 left-0 z-50 flex w-80 max-w-[88vw] flex-col border-r border-line bg-surface shadow-raised transition-transform duration-200 ${navigationOpen ? 'translate-x-0' : '-translate-x-full'}`
        }
      >
        <Navigation
          route={route}
          activeView={activeView}
          user={user}
          isAdmin={isAdmin}
          workspaces={workspaces}
          selectedWorkspace={selectedWorkspace}
          onSelectWorkspace={selectWorkspace}
          onSelectView={selectView}
          onNavigate={navigateFromNav}
          onCreateWorkspace={() => {
            setNavigationOpen(false)
            onCreateWorkspace()
          }}
          onClose={() => setNavigationOpen(false)}
          closeButtonRef={isDesktop ? undefined : drawerCloseButtonRef}
          onLogout={onLogout}
        />
      </div>

      <div className="lg:pl-[15.5rem]">
        <header
          inert={navigationOpen || notificationsOpen}
          className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-line bg-paper/95 px-4 backdrop-blur sm:px-6"
        >
          <button
            ref={menuButtonRef}
            type="button"
            className="btn-icon lg:hidden"
            aria-label={navigationOpen ? 'Navigation open' : 'Open navigation'}
            aria-controls="primary-navigation"
            aria-expanded={navigationOpen}
            onClick={() => setNavigationOpen(true)}
          >
            <Menu aria-hidden="true" size={19} />
          </button>
          <span className="flex items-center gap-2 lg:hidden">
            <BrandMark />
            <span className="text-sm font-semibold text-ink">Retriv</span>
          </span>

          <div className="hidden min-w-0 items-center gap-2 text-sm lg:flex">
            <span className="text-ink-faint">Workspaces</span>
            <span className="text-line-strong">/</span>
            <span className="truncate font-medium text-ink">
              {selectedWorkspace?.name ?? 'Select a workspace'}
            </span>
            <span className="ml-1 rounded-full border border-line bg-surface px-2 py-0.5 font-mono text-[0.65rem] uppercase tracking-eyebrow text-ink-faint">
              {selectedWorkspace?.membership_role ?? 'catalog'}
            </span>
          </div>

          <div className="ml-auto flex min-w-0 items-center gap-2">
            {showSearch ? (
              <div className="relative hidden w-64 md:block xl:w-72">
                <Search
                  aria-hidden="true"
                  className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint"
                  size={15}
                />
                <label className="sr-only" htmlFor="global-search">
                  Search documents
                </label>
                <input
                  ref={searchRef}
                  id="global-search"
                  type="search"
                  className="field h-9 py-1.5 pl-8 pr-12 text-xs"
                  value={searchQuery}
                  placeholder="Search documents…"
                  onChange={(event) => onSearchQueryChange(event.target.value)}
                />
                <kbd className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 rounded border border-line bg-sunken px-1.5 py-0.5 font-mono text-[0.6rem] text-ink-faint">
                  ⌘K
                </kbd>
              </div>
            ) : null}
            <ProcessingNotifications
              token={token}
              workspaces={workspaces}
              onUnauthorized={onUnauthorized}
              onOpenChange={setNotificationsOpen}
            />
            <span className="hidden h-8 w-px bg-line sm:block" aria-hidden="true" />
            <Avatar name={user.username} seed={user.id} size="sm" className="hidden sm:flex" />
          </div>
        </header>

        <main id="main-content" inert={navigationOpen || notificationsOpen}>
          {children}
        </main>
      </div>
    </div>
  )
}

interface NavigationProps {
  route: AppRoute
  activeView: DashboardView
  user: CurrentUser
  isAdmin: boolean
  workspaces: Workspace[]
  selectedWorkspace: Workspace | null
  onSelectWorkspace: (workspaceId: string) => void
  onSelectView: (view: DashboardView) => void
  onNavigate: (event: MouseEvent<HTMLAnchorElement>, destination: AppRoute) => void
  onCreateWorkspace: () => void
  onClose: () => void
  closeButtonRef?: React.RefObject<HTMLButtonElement | null>
  onLogout: () => void
}

function Navigation({
  route,
  activeView,
  user,
  isAdmin,
  workspaces,
  selectedWorkspace,
  onSelectWorkspace,
  onSelectView,
  onNavigate,
  onCreateWorkspace,
  onClose,
  closeButtonRef,
  onLogout,
}: NavigationProps) {
  return (
    <>
      <div className="flex items-center gap-3 border-b border-line px-4 py-4">
        <BrandLockup />
        {closeButtonRef === undefined ? null : (
          <button
            ref={closeButtonRef}
            type="button"
            className="btn-icon ml-auto"
            aria-label="Close navigation drawer"
            onClick={onClose}
          >
            <X aria-hidden="true" size={18} />
          </button>
        )}
      </div>

      <div className="border-b border-line px-3 py-3">
        <p className="px-2 eyebrow">Current workspace</p>
        <div className="mt-2 flex items-center gap-2.5 rounded-card bg-sunken/70 px-2.5 py-2">
          <Avatar
            name={selectedWorkspace?.name ?? 'Retriv'}
            seed={selectedWorkspace?.id ?? 'retriv'}
            size="sm"
          />
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-ink">
              {selectedWorkspace?.name ?? 'No workspace'}
            </p>
            <p className="font-mono text-[0.65rem] uppercase tracking-eyebrow text-ink-faint">
              {selectedWorkspace?.membership_role ?? 'Not assigned'}
            </p>
          </div>
        </div>
      </div>

      <nav className="min-h-0 flex-1 overflow-y-auto px-3 py-3" aria-label="Main navigation">
        <p className="px-2 eyebrow">Workspace</p>
        <ul className="mt-1.5 space-y-0.5">
          {WORKSPACE_NAV.map((item) => {
            const active = route === '/dashboard' && item.view === activeView
            const Icon = item.icon
            return (
              <li key={item.view}>
                <button
                  type="button"
                  className={`nav-link w-full justify-start ${active ? 'nav-link-active' : ''}`}
                  aria-current={active ? 'page' : undefined}
                  onClick={() => onSelectView(item.view)}
                >
                  <Icon aria-hidden="true" size={16} />
                  {item.label}
                </button>
              </li>
            )
          })}
          <li>
            <a
              className={route === '/upload-context' ? 'nav-link nav-link-active' : 'nav-link'}
              href="/upload-context"
              aria-label="Upload context"
              aria-current={route === '/upload-context' ? 'page' : undefined}
              onClick={(event) => onNavigate(event, '/upload-context')}
            >
              <FileUp aria-hidden="true" size={16} />
              Upload
            </a>
          </li>
        </ul>

        <p className="mt-5 px-2 eyebrow">Platform</p>
        <ul className="mt-1.5 space-y-0.5">
          <li>
            <button
              type="button"
              className={`nav-link w-full justify-start ${activeView === 'api-keys' && route === '/dashboard' ? 'nav-link-active' : ''}`}
              onClick={() => onSelectView('api-keys')}
            >
              <KeyRound aria-hidden="true" size={16} />
              API keys
            </button>
          </li>
          {isAdmin ? (
            <li>
              <button
                type="button"
                className={`nav-link w-full justify-start ${activeView === 'users' && route === '/dashboard' ? 'nav-link-active' : ''}`}
                onClick={() => onSelectView('users')}
              >
                <Users aria-hidden="true" size={16} />
                Users
              </button>
            </li>
          ) : null}
        </ul>

        <div className="mt-5 flex items-center justify-between px-2">
          <p className="eyebrow">Workspaces</p>
          <span className="font-mono text-[0.65rem] text-ink-faint">{workspaces.length}</span>
        </div>
        <ul className="mt-1.5 space-y-0.5">
          {workspaces.map((workspace) => {
            const selected = selectedWorkspace?.id === workspace.id
            return (
              <li key={workspace.id}>
                <button
                  type="button"
                  className={`flex w-full items-center gap-2.5 rounded-card px-2.5 py-2 text-left text-sm transition-colors ${
                    selected
                      ? 'bg-accent-soft text-accent-ink'
                      : 'text-ink-muted hover:bg-sunken hover:text-ink'
                  }`}
                  aria-label={`Open ${workspace.name} workspace`}
                  aria-current={selected ? 'true' : undefined}
                  onClick={() => onSelectWorkspace(workspace.id)}
                >
                  <Avatar name={workspace.name} seed={workspace.id} size="xs" />
                  <span className="min-w-0 flex-1 truncate">{workspace.name}</span>
                  {workspace.membership_role === null ? (
                    <BookOpenText aria-hidden="true" className="shrink-0 text-ink-faint" size={13} />
                  ) : null}
                </button>
              </li>
            )
          })}
        </ul>
        {isAdmin ? (
          <button
            type="button"
            className="btn btn-quiet mt-2 w-full justify-start px-2.5 text-xs"
            onClick={onCreateWorkspace}
          >
            <Plus aria-hidden="true" size={15} />
            New workspace
          </button>
        ) : null}
      </nav>

      <div className="border-t border-line p-3">
        <div className="flex items-center gap-2.5 px-1">
          <Avatar name={user.username} seed={user.id} size="md" />
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-ink">{user.username}</p>
            <p className="text-[0.7rem] capitalize text-ink-muted">{user.role}</p>
          </div>
          <button
            type="button"
            className="btn-icon ml-auto h-8 w-8 border-transparent bg-transparent"
            aria-label="Sign out"
            onClick={onLogout}
          >
            <LogOut aria-hidden="true" size={15} />
          </button>
        </div>
      </div>
    </>
  )
}
