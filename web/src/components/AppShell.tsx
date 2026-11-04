import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import { ArrowLeftRight, Check, ChevronsDown, FileText, FileUp, KeyRound, LayoutGrid, LockKeyhole, LogOut, Menu, MessageSquare, Plus, Sparkles, Users, X } from 'lucide-react'
import type { AppRoute, CurrentUser, DashboardView, DocumentSearchHit, Workspace } from '../types'
import type { ProcessingRuns } from '../hooks/useProcessingRuns'
import { ConfirmDialog } from './Dialog'
import { ThemeMenu } from './ThemeMenu'
import { DocumentSearch } from './DocumentSearch'
import { PipelinePopover } from './PipelinePopover'
import { ProcessingNotifications } from './ProcessingNotifications'
import { Avatar } from './ui/Avatar'
import { BrandLockup, BrandMark } from './ui/Brand'
import type { ThemePreference } from '../hooks/useTheme'
import { useIsDesktop } from '../hooks/useIsDesktop'

interface AppShellProps {
  route: AppRoute
  activeView: DashboardView
  onNavigate: (route: AppRoute) => void
  onSelectView: (view: DashboardView) => void
  onSelectWorkspace: (workspaceId: string) => void
  onCreateWorkspace: () => void
  adminMode: boolean
  user: CurrentUser
  workspaces: Workspace[]
  runs: ProcessingRuns
  selectedWorkspace: Workspace | null
  searchQuery: string
  onSearchQueryChange: (query: string) => void
  onOpenDocument: (hit: DocumentSearchHit) => void
  onUnauthorized: () => void
  onLogout: () => void
  themePreference: ThemePreference
  onThemeChange: (preference: ThemePreference) => void
  children: ReactNode
}

const WORKSPACE_NAV: Array<{ view: DashboardView; label: string; icon: typeof LayoutGrid }> = [
  { view: 'chat', label: 'Chat', icon: MessageSquare },
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
  adminMode,
  user,
  workspaces,
  runs,
  selectedWorkspace,
  searchQuery,
  onSearchQueryChange,
  onOpenDocument,
  onUnauthorized,
  onLogout,
  themePreference,
  onThemeChange,
  children,
}: AppShellProps) {
  const [navigationOpen, setNavigationOpen] = useState(false)
  const [openPopover, setOpenPopover] = useState<'notifications' | 'pipeline' | null>(null)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const drawerCloseButtonRef = useRef<HTMLButtonElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const isDesktop = useIsDesktop()
  const isAdmin = user.role === 'admin'
  const showSearch =
    route === '/dashboard'
    && !adminMode
    && activeView !== 'api-keys'
    && activeView !== 'users'
    && activeView !== 'agent-model'
    && activeView !== 'chat'

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
          className="fixed inset-0 z-40 cursor-pointer bg-scrim backdrop-blur-[1px]"
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
          adminMode={adminMode}
          onClose={() => setNavigationOpen(false)}
          closeButtonRef={isDesktop ? undefined : drawerCloseButtonRef}
          onLogout={onLogout}
          themePreference={themePreference}
          onThemeChange={onThemeChange}
        />
      </div>

      <div className="lg:pl-[15.5rem]">
        <header
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
            {adminMode ? (
              <span className="truncate font-medium text-ink">All workspaces</span>
            ) : (
              <>
                <span className="truncate font-medium text-ink">
                  {selectedWorkspace?.name ?? 'Select a workspace'}
                </span>
                <span className="ml-1 stamp">{selectedWorkspace?.membership_role ?? 'catalog'}</span>
              </>
            )}
          </div>

          <div className="ml-auto flex min-w-0 items-center gap-2">
          {showSearch ? (
            <DocumentSearch
              value={searchQuery}
              onChange={onSearchQueryChange}
              onOpenDocument={onOpenDocument}
              onUnauthorized={onUnauthorized}
              inputRef={searchRef}
            />
          ) : null}
            <PipelinePopover
              runs={runs}
              workspaceId={selectedWorkspace?.id ?? null}
              open={openPopover === 'pipeline'}
              onOpenChange={(open) => setOpenPopover(open ? 'pipeline' : null)}
            />
            <ProcessingNotifications
              workspaces={workspaces}
              runs={runs}
              open={openPopover === 'notifications'}
              onOpenChange={(open) => setOpenPopover(open ? 'notifications' : null)}
            />
            <span className="hidden h-8 w-px bg-line sm:block" aria-hidden="true" />
            <Avatar name={user.username} seed={user.id} size="sm" className="hidden sm:flex" />
          </div>
        </header>

        <main id="main-content" inert={navigationOpen}>
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
  adminMode: boolean
  workspaces: Workspace[]
  selectedWorkspace: Workspace | null
  onSelectWorkspace: (workspaceId: string) => void
  onSelectView: (view: DashboardView) => void
  onNavigate: (event: MouseEvent<HTMLAnchorElement>, destination: AppRoute) => void
  onCreateWorkspace: () => void
  onClose: () => void
  closeButtonRef?: React.RefObject<HTMLButtonElement | null>
  onLogout: () => void
  themePreference: ThemePreference
  onThemeChange: (preference: ThemePreference) => void
}

function Navigation({
  route,
  activeView,
  user,
  isAdmin,
  adminMode,
  workspaces,
  selectedWorkspace,
  onSelectWorkspace,
  onSelectView,
  onNavigate,
  onCreateWorkspace,
  onClose,
  closeButtonRef,
  onLogout,
  themePreference,
  onThemeChange,
}: NavigationProps) {
  const hasMembership = workspaces.some((workspace) => workspace.membership_role !== null)
  const [directoryOpen, setDirectoryOpen] = useState(false)
  const [signOutOpen, setSignOutOpen] = useState(false)
  const [systemDark, setSystemDark] = useState(
    () => typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-color-scheme: dark)').matches,
  )

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const update = () => setSystemDark(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])

  const themeResolved =
    themePreference === 'system' ? (systemDark ? 'dark' : 'light') : themePreference
  const memberWorkspaces = workspaces.filter((workspace) => workspace.membership_role !== null)
  const catalogWorkspaces = workspaces.filter((workspace) => workspace.membership_role === null)
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

      {adminMode ? null : (
        <div className="border-b border-line px-3 py-3">
          <p className="px-2 eyebrow">Current workspace</p>
          <WorkspaceSwitcher
            workspaces={memberWorkspaces}
            selectedWorkspace={selectedWorkspace}
            onSelect={onSelectWorkspace}
          />
        </div>
      )}

      <nav className="min-h-0 flex-1 overflow-y-auto px-3 py-3" aria-label="Main navigation">
        {adminMode ? null : (
          <>
            <p className="px-2 eyebrow">Workspace</p>
            <ul className="mt-1.5 space-y-0.5">
              {WORKSPACE_NAV.filter((item) => item.view !== 'chat' || hasMembership).map((item) => {
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
          </>
        )}

        <p className={`px-2 eyebrow ${adminMode ? '' : 'mt-5'}`}>Platform</p>
        <ul className="mt-1.5 space-y-0.5">
          {adminMode ? null : (
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
          )}
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
          {isAdmin ? (
            <li>
              <button
                type="button"
                className={`nav-link w-full justify-start ${activeView === 'agent-model' && route === '/dashboard' ? 'nav-link-active' : ''}`}
                onClick={() => onSelectView('agent-model')}
              >
                <Sparkles aria-hidden="true" size={16} />
                Agent model
              </button>
            </li>
          ) : null}
        </ul>

        <div className="mt-5 flex items-center justify-between px-2">
          <p className="eyebrow">Workspaces</p>
          <span className="font-mono text-[0.65rem] text-ink-faint">
            {adminMode ? workspaces.length : memberWorkspaces.length}
          </span>
        </div>
        {adminMode ? (
          <button
            type="button"
            className={`nav-link mt-1.5 w-full justify-start ${
              route === '/dashboard' && activeView === 'admin-overview' ? 'nav-link-active' : ''
            }`}
            aria-current={
              route === '/dashboard' && activeView === 'admin-overview' ? 'page' : undefined
            }
            onClick={() => onSelectView('admin-overview')}
          >
            <LayoutGrid aria-hidden="true" size={16} />
            All workspaces
          </button>
        ) : null}
        {isAdmin ? (
          <button
            type="button"
            className="mt-1.5 flex w-full items-center gap-2.5 rounded-control px-2.5 py-2 text-left text-sm text-ink-muted transition-colors hover:bg-sunken hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-ink"
            onClick={onCreateWorkspace}
          >
            <Plus aria-hidden="true" size={15} />
            New workspace
          </button>
        ) : null}
        {adminMode ? (
          <ul className="mt-1.5 space-y-0.5">
            {workspaces.map((workspace) => (
              <li
                key={workspace.id}
                className="flex items-center gap-2.5 rounded-control px-2.5 py-2 text-sm text-ink-faint"
              >
                <Avatar name={workspace.name} seed={workspace.id} size="xs" />
                <span className="min-w-0 flex-1 truncate">{workspace.name}</span>
                <LockKeyhole aria-hidden="true" className="shrink-0" size={12} />
              </li>
            ))}
          </ul>
        ) : (
          <>
            <ul className="mt-1.5 space-y-0.5">
              {memberWorkspaces.map((workspace) => {
                const selected = selectedWorkspace?.id === workspace.id
                return (
                  <li key={workspace.id}>
                    <button
                      type="button"
                      className={`flex w-full items-center gap-2.5 rounded-control px-2.5 py-2 text-left text-sm transition-colors ${
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
                    </button>
                  </li>
                )
              })}
            </ul>
            {catalogWorkspaces.length > 0 ? (
              <>
                <button
                  type="button"
                  className="mt-1 flex h-[22px] w-full items-center justify-center rounded-control text-ink-faint transition-colors hover:bg-sunken hover:text-ink-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-ink"
                  aria-label={
                    directoryOpen ? 'Collapse catalogued workspaces' : 'Show catalogued workspaces'
                  }
                  aria-expanded={directoryOpen}
                  onClick={() => setDirectoryOpen((open) => !open)}
                >
                  <ChevronsDown
                    aria-hidden="true"
                    size={13}
                    className={`transition-transform duration-300 ${directoryOpen ? 'rotate-180' : ''}`}
                  />
                </button>
                <div
                  aria-hidden={!directoryOpen}
                  className={`grid transition-[grid-template-rows] duration-300 ease-out ${
                    directoryOpen ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'
                  }`}
                >
                  <div className="overflow-hidden">
                    <ul className="space-y-0.5 pt-1">
                      {catalogWorkspaces.map((workspace) => {
                        const selected = selectedWorkspace?.id === workspace.id
                        return (
                          <li key={workspace.id}>
                            <button
                              type="button"
                              className={`flex w-full items-center gap-2.5 rounded-control px-2.5 py-2 text-left text-sm transition-colors ${
                                selected
                                  ? 'bg-sunken text-ink-muted'
                                  : 'text-ink-faint hover:bg-sunken hover:text-ink-muted'
                              }`}
                              aria-label={`Open ${workspace.name} workspace`}
                              aria-current={selected ? 'true' : undefined}
                              onClick={() => onSelectWorkspace(workspace.id)}
                            >
                              <Avatar name={workspace.name} seed={workspace.id} size="xs" />
                              <span className="min-w-0 flex-1 truncate">{workspace.name}</span>
                              <LockKeyhole aria-hidden="true" className="shrink-0" size={12} />
                            </button>
                          </li>
                        )
                      })}
                    </ul>
                  </div>
                </div>
              </>
            ) : null}
          </>
        )}
      </nav>

      <div className="border-t border-line p-3">
        <div className="flex items-center gap-2.5 px-1">
          <Avatar name={user.username} seed={user.id} size="md" />
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-ink">{user.username}</p>
            <p className="text-[0.7rem] capitalize text-ink-muted">{user.role}</p>
          </div>
          <div className="ml-auto flex items-center gap-0.5">
            <ThemeMenu preference={themePreference} resolved={themeResolved} onSelect={onThemeChange} />
            <button
              type="button"
              className="btn-icon h-8 w-8 border-transparent bg-transparent"
              aria-label="Sign out"
              onClick={() => setSignOutOpen(true)}
            >
              <LogOut aria-hidden="true" size={15} />
            </button>
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={signOutOpen}
        title="Sign out of Retriv?"
        description="Your session ends in this tab. Unsaved work in an open form is lost, and you will need to sign in again to reach the dashboard."
        confirmLabel="Sign out"
        destructive
        onClose={() => setSignOutOpen(false)}
        onConfirm={onLogout}
      />
    </>
  )
}

interface WorkspaceSwitcherProps {
  workspaces: Workspace[]
  selectedWorkspace: Workspace | null
  onSelect: (workspaceId: string) => void
}

/**
 * The current workspace, and the switch that changes it.
 *
 * The card itself is the button: with only a handful of memberships, a
 * dropdown beside it beats walking down to the workspace list. Picking one
 * closes the menu in the same click and keeps the current view, which is
 * the workspace-scoped behaviour the dashboard already implements.
 */
function WorkspaceSwitcher({
  workspaces,
  selectedWorkspace,
  onSelect,
}: WorkspaceSwitcherProps) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    function closeOnPointerDown(event: PointerEvent) {
      if (containerRef.current?.contains(event.target as Node)) return
      setOpen(false)
    }

    window.addEventListener('keydown', closeOnEscape)
    document.addEventListener('pointerdown', closeOnPointerDown)
    return () => {
      window.removeEventListener('keydown', closeOnEscape)
      document.removeEventListener('pointerdown', closeOnPointerDown)
    }
  }, [open])

  const hasChoices = workspaces.length > 1

  return (
    <div ref={containerRef} className="relative mt-2">
      <button
        type="button"
        className="flex w-full items-center gap-2.5 rounded-control bg-sunken/70 px-2.5 py-2 text-left transition-colors hover:bg-sunken focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-ink"
        aria-haspopup={hasChoices ? 'menu' : undefined}
        aria-expanded={hasChoices ? open : undefined}
        aria-label={
          hasChoices
            ? `Current workspace ${selectedWorkspace?.name ?? 'none'}, switch workspace`
            : `Current workspace ${selectedWorkspace?.name ?? 'none'}`
        }
        disabled={!hasChoices}
        onClick={() => setOpen((current) => !current)}
      >
        <Avatar
          name={selectedWorkspace?.name ?? 'Retriv'}
          seed={selectedWorkspace?.id ?? 'retriv'}
          size="sm"
        />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-ink">
            {selectedWorkspace?.name ?? 'No workspace'}
          </span>
          <span className="block font-mono text-[0.65rem] text-ink-faint">
            {selectedWorkspace?.membership_role ?? 'Not assigned'}
          </span>
        </span>
        {hasChoices ? (
          <ArrowLeftRight
            aria-hidden="true"
            className={`shrink-0 text-ink-faint transition-transform duration-200 ${open ? 'text-accent-ink' : ''}`}
            size={14}
          />
        ) : null}
      </button>

      {open && hasChoices ? (
        <div
          role="menu"
          aria-label="Switch workspace"
          className="popover-enter absolute inset-x-0 top-[calc(100%+6px)] z-40 overflow-hidden rounded-control border border-line bg-surface py-1 shadow-raised"
        >
          {workspaces.map((workspace) => {
            const active = workspace.id === selectedWorkspace?.id
            return (
              <button
                key={workspace.id}
                type="button"
                role="menuitemradio"
                aria-checked={active}
                className={`flex w-full items-center gap-2.5 px-2.5 py-2 text-left text-sm transition-colors hover:bg-sunken ${
                  active ? 'text-accent-ink' : 'text-ink-muted'
                }`}
                aria-label={`Switch to ${workspace.name}`}
                onClick={() => {
                  setOpen(false)
                  onSelect(workspace.id)
                }}
              >
                <Avatar name={workspace.name} seed={workspace.id} size="xs" />
                <span className="min-w-0 flex-1 truncate">{workspace.name}</span>
                <span className="shrink-0 font-mono text-[0.6rem] text-ink-faint">
                  {workspace.membership_role}
                </span>
                {active ? <Check aria-hidden="true" size={13} /> : null}
              </button>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}
