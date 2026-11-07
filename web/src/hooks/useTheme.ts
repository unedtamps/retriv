import { useCallback, useEffect, useState } from 'react'

export type ThemePreference = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'

const STORAGE_KEY = 'retriv.theme'
const DARK_QUERY = '(prefers-color-scheme: dark)'

const THEME_COLOR: Record<ResolvedTheme, string> = {
  light: '#E4EAF1',
  dark: '#0B1017',
}

function readPreference(): ThemePreference {
  if (typeof localStorage === 'undefined') return 'system'
  const stored = localStorage.getItem(STORAGE_KEY)
  return stored === 'light' || stored === 'dark' ? stored : 'system'
}

function systemPrefersDark(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia(DARK_QUERY).matches
}

/**
 * The colour theme, and the preference behind it.
 *
 * `system` is the default and follows the OS live, so a user who has never
 * touched the control gets the theme their machine already asked for. An
 * explicit choice wins over the OS and is remembered across sessions.
 *
 * The theme is applied by stamping `data-theme` on <html>; every colour in the
 * app already resolves through a `--color-*` variable, so that one attribute
 * repaints the whole interface. `color-scheme` goes with it so native
 * scrollbars and form controls follow, and `theme-color` so the browser chrome
 * matches when the app is installed.
 */
export function useTheme() {
  const [preference, setPreferenceState] = useState<ThemePreference>(readPreference)
  const [systemDark, setSystemDark] = useState(systemPrefersDark)

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const media = window.matchMedia(DARK_QUERY)
    const update = () => setSystemDark(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])

  const resolved: ResolvedTheme =
    preference === 'system' ? (systemDark ? 'dark' : 'light') : preference

  useEffect(() => {
    const root = document.documentElement
    root.dataset.theme = resolved
    root.style.colorScheme = resolved
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute('content', THEME_COLOR[resolved])
  }, [resolved])

  const setPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next)
    if (next === 'system') localStorage.removeItem(STORAGE_KEY)
    else localStorage.setItem(STORAGE_KEY, next)
  }, [])

  return { preference, resolved, setPreference }
}
