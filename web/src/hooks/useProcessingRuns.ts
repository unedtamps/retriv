import { api } from '../api'
import type { ProcessingRun } from '../types'
import { useApiResource } from './useApiResource'

export interface ProcessingRuns {
  activeRun: ProcessingRun | null
  recentRuns: ProcessingRun[]
  loading: boolean
  error: string | null
  reload: () => void
}

/**
 * The runs the pipeline is working on, and the ones it worked on recently.
 *
 * There is no polling timer. An idle page used to re-request both endpoints
 * every ten seconds forever, which is a request per user per ten seconds for
 * data that usually has not changed. This fetches once, and again only when
 * someone asks for it.
 */
export function useProcessingRuns(
  token: string,
  onUnauthorized: () => void,
): ProcessingRuns {
  const runs = useApiResource(
    async (signal) => {
      const [active, recent] = await Promise.all([
        api.getActiveProcessingRun(token, signal),
        api.listRecentProcessingRuns(token, signal),
      ])
      return { activeRun: active.item, recentRuns: recent.items }
    },
    [token],
    { onUnauthorized },
  )

  return {
    activeRun: runs.data?.activeRun ?? null,
    recentRuns: runs.data?.recentRuns ?? [],
    loading: runs.loading,
    error: runs.error,
    reload: runs.reload,
  }
}
