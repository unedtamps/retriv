import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import { FileUp, LayoutGrid, LogOut, Menu, Users, X } from 'lucide-react'
import type { AppRoute, CurrentUser, Workspace } from '../types'
import { ProcessingNotifications } from './ProcessingNotifications'
import { BrandLockup, BrandMark } from './ui/Brand'
import { useIsDesktop } from '../hooks/useIsDesktop'

interface AppShellProps {
  route: AppRoute
  onNavigate: (route: AppRoute) => void
  token: string
  user: CurrentUser
  workspaces: Workspace[]
  onUnauthorized: () => void
  onLogout: () => void
  children: ReactNode
}

const ROUTE_LABELS: Record<AppRoute, string> = {
  '/dashboard': 'Workspace governance',
  '/upload-context': 'Document ingestion',
}

/**
 * The frame around every page: a rail on a wide screen, a drawer on a narrow
 * one, and a bar across the top.
 *
 * The navigation used to be an off-canvas drawer at every width, which meant
 * that on a 1536px monitor the whole chrome hid behind a hamburger button.
 * The same navigation is now a permanent rail from `lg` up, and the drawer
 * is what a phone gets.
 */
export function AppShell({
  route,
  onNavigate,
  token,
  user,
  workspaces,
  onUnauthorized,
  onLogout,
  children,
}: AppShellProps) {
  const [navigationOpen, setNavigationOpen] = useState(false)
  const [notificationsOpen, setNotificationsOpen] = useState(false)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const drawerCloseButtonRef = useRef<HTMLButtonElement>(null)
  const isDesktop = useIsDesktop()

  const isAdmin = user.role === 'admin'

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

  function navigateFromNav(event: MouseEvent<HTMLAnchorElement>, destination: AppRoute) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    setNavigationOpen(false)
    onNavigate(destination)
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

      {/*
        One navigation, two shapes. On a wide screen it is a permanent rail and a
        navigation landmark; on a narrow one it is an off-canvas drawer and a
        modal dialog. The role follows the viewport, because a landmark that
        is a dialog would be a lie, and a dialog that is a landmark would be
        unreachable.
      */}
      <div
        role={isDesktop ? undefined : 'dialog'}
        aria-label={isDesktop ? undefined : 'Primary navigation'}
        aria-modal={isDesktop ? undefined : true}
        aria-hidden={isDesktop ? undefined : !navigationOpen}
        inert={isDesktop ? undefined : !navigationOpen}
        className={
          isDesktop
            ? 'fixed inset-y-0 left-0 z-30 flex w-60 flex-col border-r border-line bg-surface'
            : `fixed inset-y-0 left-0 z-50 flex w-72 max-w-[85vw] flex-col border-r border-line bg-surface shadow-raised transition-transform duration-200 ${navigationOpen ? 'translate-x-0' : '-translate-x-full'}`
        }
      >
        <Navigation
          route={route}
          isAdmin={isAdmin}
          user={user}
          onNavigate={navigateFromNav}
          onClose={() => setNavigationOpen(false)}
          closeButtonRef={isDesktop ? undefined : drawerCloseButtonRef}
          onLogout={onLogout}
        />
      </div>

      <div className="lg:pl-60">
        <header
          inert={navigationOpen || notificationsOpen}
          className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-line bg-paper/95 px-4 backdrop-blur sm:px-6"
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
          <span className="eyebrow hidden sm:block">{ROUTE_LABELS[route]}</span>
          <div className="ml-auto flex items-center gap-2">
            <ProcessingNotifications
              token={token}
              workspaces={workspaces}
              onUnauthorized={onUnauthorized}
              onOpenChange={setNotificationsOpen}
            />
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
  isAdmin: boolean
  user: CurrentUser
  onNavigate: (event: MouseEvent<HTMLAnchorElement>, destination: AppRoute) => void
  onClose: () => void
  closeButtonRef?: React.RefObject<HTMLButtonElement | null>
  onLogout: () => void
}

function Navigation({
  route,
  isAdmin,
  user,
  onNavigate,
  onClose,
  closeButtonRef,
  onLogout,
}: NavigationProps) {
  // The navigation is for the screens. API keys are a section of the
  // dashboard, not a screen of their own, so they are reached from the page
  // rather than from here.
  const items = [
    { to: '/dashboard' as AppRoute, label: 'Dashboard', icon: LayoutGrid },
    { to: '/upload-context' as AppRoute, label: 'Upload context', icon: FileUp },
    ...(isAdmin ? [{ to: '/dashboard#users' as const, label: 'Users', icon: Users }] : []),
  ]

  return (
    <>
      <div className="flex items-center gap-3 border-b border-line px-5 py-4">
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

      <nav className="flex-1 overflow-y-auto p-3" aria-label="Main navigation">
        <ul className="space-y-1">
          {items.map((item) => {
            const active = item.to === route
            const Icon = item.icon
            return (
              <li key={item.label}>
                <a
                  className={active ? 'nav-link nav-link-active' : 'nav-link'}
                  href={item.to}
                  aria-current={active ? 'page' : undefined}
                  onClick={(event) => {
                    if (item.to.includes('#')) {
                      onClose()
                      return
                    }
                    onNavigate(event, item.to as AppRoute)
                  }}
                >
                  <Icon aria-hidden="true" size={17} />
                  {item.label}
                </a>
              </li>
            )
          })}
        </ul>
      </nav>

      <div className="border-t border-line p-4">
        <p className="eyebrow">Signed in as</p>
        <div className="mt-2 flex items-center gap-3">
          <span className="flex h-8 w-8 items-center justify-center rounded-card bg-accent-soft text-xs font-semibold uppercase text-accent-ink">
            {user.username.slice(0, 2)}
          </span>
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-ink">{user.username}</p>
            <p className="text-xs capitalize text-ink-muted">{user.role}</p>
          </div>
        </div>
        <button
          type="button"
          className="btn btn-quiet mt-3 w-full justify-start"
          onClick={onLogout}
        >
          <LogOut aria-hidden="true" size={16} />
          Sign out
        </button>
      </div>
    </>
  )
}
