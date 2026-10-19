import { useEffect, useState } from 'react'

const WIDE_SCREEN = '(min-width: 1024px)'

/**
 * Whether the viewport is wide enough for the permanent rail.
 *
 * The navigation is one element: a rail on a wide screen, an off-canvas
 * drawer on a narrow one. Which one it is decides whether it is a
 * navigation landmark or a modal dialog, and that is not something CSS can
 * decide for us.
 */
export function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState(() => currentMatch()?.matches ?? false)

  useEffect(() => {
    const media = currentMatch()
    if (media === null) return
    const update = () => setIsDesktop(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])

  return isDesktop
}

function currentMatch(): MediaQueryList | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
  return window.matchMedia(WIDE_SCREEN)
}
