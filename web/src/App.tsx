import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, LoaderCircle, RefreshCw } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from './api'
import { Dashboard } from './components/Dashboard'
import { LoginScreen } from './components/LoginScreen'
import { BrandMark } from './components/ui/Brand'
import { clearSessionToken, readSessionToken, writeSessionToken } from './session'
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

function App() {
  const [route, setRoute] = useState<AppRoute>(currentRoute)
  const [token, setToken] = useState<string | null>(() => readSessionToken())
  const [ready, setReady] = useState<ReadyState | null>(null)
  const [loading, setLoading] = useState(token !== null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [retryKey, setRetryKey] = useState(0)

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

  const navigate = useCallback((route: AppRoute) => {
    if (window.location.pathname !== route) window.history.pushState(null, '', route)
    setRoute(route)
  }, [])

  useEffect(() => {
    if (token === null) return

    const controller = new AbortController()
    Promise.all([
      api.me(token, controller.signal),
      loadWorkspaceCatalog(token, controller.signal),
    ])
      .then(([user, workspaces]) => {
        setReady({ user, workspaces })
        setLoading(false)
      })
      .catch((caught: unknown) => {
        if (caught instanceof Error && caught.name === 'AbortError') return
        if (isUnauthorized(caught)) {
          clearSessionToken()
          setToken(null)
          return
        }
        setLoadError(getErrorMessage(caught))
        setLoading(false)
      })
    return () => controller.abort()
  }, [token, retryKey])

  async function handleLogin(username: string, password: string) {
    const session = await api.login(username, password)
    writeSessionToken(session.access_token)
    setLoading(true)
    setLoadError(null)
    setToken(session.access_token)
  }

  function retryLoad() {
    setLoading(true)
    setLoadError(null)
    setRetryKey((value) => value + 1)
  }

  const handleLogout = useCallback(() => {
    clearSessionToken()
    setToken(null)
    setReady(null)
    setLoading(false)
    setLoadError(null)
  }, [])

  const refreshWorkspaces = useCallback(async () => {
    if (token === null) return
    try {
      const workspaces = await loadWorkspaceCatalog(token)
      setReady((current) => current === null ? current : { ...current, workspaces })
    } catch (caught) {
      if (isUnauthorized(caught)) handleLogout()
      else throw caught
    }
  }, [token, handleLogout])

  if (token === null) return <LoginScreen onLogin={handleLogin} />
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
            <button type="button" className="btn btn-secondary" onClick={handleLogout}>
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
      token={token}
      user={ready.user}
      workspaces={ready.workspaces}
      onRefreshWorkspaces={refreshWorkspaces}
      onUnauthorized={handleLogout}
      onLogout={handleLogout}
    />
  )
}

async function loadWorkspaceCatalog(token: string, signal?: AbortSignal): Promise<Workspace[]> {
  const pageSize = 100
  const first = await api.listWorkspaces(token, { limit: pageSize, signal })
  if (first.items.length >= first.total) return first.items

  const remainingPages: Promise<{ items: Workspace[] }>[] = []
  for (let offset = pageSize; offset < first.total; offset += pageSize) {
    remainingPages.push(api.listWorkspaces(token, { offset, limit: pageSize, signal }))
  }
  const pages = await Promise.all(remainingPages)
  return [first, ...pages].flatMap((page) => page.items)
}

function BootScreen() {
  return (
    <main className="flex min-h-svh items-center justify-center bg-paper">
      <div className="text-center">
        <span className="mx-auto flex h-10 w-10 items-center justify-center rounded-card bg-accent text-accent-ink">
          <BrandMark size={19} />
        </span>
        <LoaderCircle className="mx-auto mt-4 animate-spin text-ink-faint" aria-hidden="true" size={22} />
        <p className="mt-3 font-mono text-eyebrow uppercase tracking-eyebrow text-ink-faint">
          Resolving access policy
        </p>
      </div>
    </main>
  )
}

export default App
