import { api } from '../api'
import type { ProcessingRun } from '../types'
import { useApiResource } from './useApiResource'

export interface ProcessingRuns {
  runs: ProcessingRun[]
  loading: boolean
  error: string | null
  reload: () => void
}

/**
 * The runs the pipeline is working on, across every readable workspace.
 *
 * There is no polling timer. An idle page used to re-request the feed every
 * ten seconds forever, which is a request per user per ten seconds for data
 * that usually has not changed. This fetches once, and again only when
 * someone asks for it.
 */
export function useProcessingRuns(
  token: string,
  onUnauthorized: () => void,
): ProcessingRuns {
  const resource = useApiResource(
    (signal) => api.listProcessingActivity(token, signal),
    [token],
    { onUnauthorized },
  )

  return {
    runs: resource.data?.items ?? [],
    loading: resource.loading,
    error: resource.error,
    reload: resource.reload,
  }
}
