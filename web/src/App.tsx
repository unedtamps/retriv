import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, LoaderCircle, RefreshCw } from 'lucide-react'
import {
  api,
  clearTokens,
  getErrorMessage,
  hasSession,
  isUnauthorized,
  restoreSession,
  setSessionLostHandler,
} from './api'
import { Dashboard } from './components/Dashboard'
import { LoginScreen } from './components/LoginScreen'
import { BrandMark } from './components/ui/Brand'
import { useTheme } from './hooks/useTheme'
import { clearChatHistory } from './lib/chatHistoryStore'
import { clearDocumentCache } from './lib/documentCache'
import type { AppRoute, CurrentUser, Workspace } from './types'

interface ReadyState {
  user: CurrentUser
  workspaces: Workspace[]
}

function currentRoute(): AppRoute {
  if (window.location.pathname === '/upload-context') return '/upload-context'
  if (window.location.pathname === '/dashboard') return '/dashboard'
  return '/dashboard'
}

/**
 * The app shell: resolve a session, then hand everything to the dashboard.
 *
 * The tokens themselves are not React state. They belong to `api.ts`, which
 * renews them mid-request when it has to; what React watches here is only
 * whether a session exists at all, which is what decides between the sign-in
 * screen and the dashboard.
 */
function App() {
  const [route, setRoute] = useState<AppRoute>(currentRoute)
  const [signedIn, setSignedIn] = useState(() => restoreSession())
  const [ready, setReady] = useState<ReadyState | null>(null)
  const [loading, setLoading] = useState(hasSession())
  const [loadError, setLoadError] = useState<string | null>(null)
  const [retryKey, setRetryKey] = useState(0)
  const theme = useTheme()

  useEffect(() => {
    if (
      window.location.pathname !== '/dashboard'
      && window.location.pathname !== '/upload-context'
    ) {
      window.history.replaceState(null, '', '/dashboard')
    }
    const syncRoute = () => setRoute(currentRoute())
    window.addEventListener('popstate', syncRoute)
    return () => window.removeEventListener('popstate', syncRoute)
  }, [])

  const handleLogout = useCallback(() => {
    clearChatHistory()
    clearDocumentCache()
    clearTokens()
    setSignedIn(false)
    setReady(null)
    setLoading(false)
    setLoadError(null)
  }, [])

  useEffect(() => {
    setSessionLostHandler(handleLogout)
    return () => setSessionLostHandler(null)
  }, [handleLogout])

  const navigate = useCallback((route: AppRoute) => {
    if (window.location.pathname !== route) window.history.pushState(null, '', route)
    setRoute(route)
  }, [])

  useEffect(() => {
    if (!signedIn) return

    const controller = new AbortController()
    Promise.all([
      api.me(controller.signal),
      loadWorkspaceCatalog(controller.signal),
    ])
      .then(([user, workspaces]) => {
        setReady({ user, workspaces })
        setLoading(false)
      })
      .catch((caught: unknown) => {
        if (caught instanceof Error && caught.name === 'AbortError') return
        if (isUnauthorized(caught)) {
          clearTokens()
          setSignedIn(false)
          return
        }
        setLoadError(getErrorMessage(caught))
        setLoading(false)
      })
    return () => controller.abort()
  }, [signedIn, retryKey])

  async function handleLogin(username: string, password: string) {
    await api.login(username, password)
    setLoading(true)
    setLoadError(null)
    setSignedIn(true)
  }

  async function handleSignOut() {
    await api.logout()
    handleLogout()
  }

  function retryLoad() {
    setLoading(true)
    setLoadError(null)
    setRetryKey((value) => value + 1)
  }

  const refreshWorkspaces = useCallback(async () => {
    try {
      const workspaces = await loadWorkspaceCatalog()
      setReady((current) => current === null ? current : { ...current, workspaces })
    } catch (caught) {
      if (isUnauthorized(caught)) handleLogout()
      else throw caught
    }
  }, [handleLogout])

  if (!signedIn) return <LoginScreen onLogin={handleLogin} />
  if (loading) return <BootScreen />
  if (loadError !== null || ready === null) {
    return (
      <main className="flex min-h-svh items-center justify-center bg-paper p-5">
        <section className="w-full max-w-md rounded-card border border-line bg-surface p-6">
          <span className="flex h-10 w-10 items-center justify-center rounded-card border border-danger/30 bg-danger-soft text-danger">
            <AlertTriangle aria-hidden="true" size={19} />
          </span>
          <h1 className="mt-4 text-lg font-semibold text-ink">Dashboard unavailable</h1>
          <p className="mt-2 text-sm leading-6 text-ink-muted">
            {loadError ?? 'The dashboard could not be loaded.'}
          </p>
          <div className="mt-5 flex gap-2">
            <button type="button" className="btn btn-primary" onClick={retryLoad}>
              <RefreshCw aria-hidden="true" size={16} />
              Try again
            </button>
            <button type="button" className="btn btn-secondary" onClick={handleSignOut}>
              Sign out
            </button>
          </div>
        </section>
      </main>
    )
  }

  return (
    <Dashboard
      route={route}
      onNavigate={navigate}
      user={ready.user}
      workspaces={ready.workspaces}
      onRefreshWorkspaces={refreshWorkspaces}
      onUnauthorized={handleLogout}
      onLogout={handleSignOut}
      themePreference={theme.preference}
      onThemeChange={theme.setPreference}
    />
  )
}

async function loadWorkspaceCatalog(signal?: AbortSignal): Promise<Workspace[]> {
  const pageSize = 100
  const first = await api.listWorkspaces({ limit: pageSize, signal })
  if (first.items.length >= first.total) return first.items

  const remainingPages: Promise<{ items: Workspace[] }>[] = []
  for (let offset = pageSize; offset < first.total; offset += pageSize) {
    remainingPages.push(api.listWorkspaces({ offset, limit: pageSize, signal }))
  }
  const pages = await Promise.all(remainingPages)
  return [first, ...pages].flatMap((page) => page.items)
}

function BootScreen() {
  return (
    <main className="flex min-h-svh items-center justify-center bg-paper">
      <div className="text-center">
        <span className="mx-auto flex h-10 w-10 items-center justify-center rounded-card bg-accent text-white">
          <BrandMark size={19} />
        </span>
        <LoaderCircle className="mx-auto mt-4 animate-spin text-ink-faint" aria-hidden="true" size={22} />
        <p className="mt-3 text-xs text-ink-faint">Resolving access policy</p>
      </div>
    </main>
  )
}

export default App
