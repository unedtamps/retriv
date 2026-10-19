import { useCallback, useEffect, useRef, useState } from 'react'
import { getErrorMessage, isUnauthorized } from '../api'

export interface ApiResource<T> {
  data: T | null
  loading: boolean
  error: string | null
  reload: () => void
  /** Apply a local change to what was loaded, without asking the server again. */
  setData: (value: T | ((current: T | null) => T | null)) => void
}

/**
 * One fetch, with the abort and the sign-out handling the app needs.
 *
 * This was written out seven times, in seven files, and every copy was a
 * little different: some cancelled on unmount, some did not, and one of them
 * set a state after the component was gone. Here it is once.
 *
 * The loader is re-run when `deps` change, and on demand through `reload`.
 */
export function useApiResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  options: { onUnauthorized?: () => void } = {},
): ApiResource<T> {
  const { onUnauthorized } = options
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)

    load(controller.signal)
      .then((result) => {
        if (!mounted.current) return
        setData(result)
        setError(null)
      })
      .catch((caught: unknown) => {
        if (!mounted.current) return
        if (caught instanceof Error && caught.name === 'AbortError') return
        if (isUnauthorized(caught)) {
          onUnauthorized?.()
          return
        }
        setError(getErrorMessage(caught))
      })
      .finally(() => {
        if (mounted.current) setLoading(false)
      })

    return () => controller.abort()
    // The loader is a closure over the values that identify the request, so
    // the caller's dependency list is what decides when it runs again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, refreshKey])

  const reload = useCallback(() => setRefreshKey((key) => key + 1), [])

  return { data, loading, error, reload, setData }
}
