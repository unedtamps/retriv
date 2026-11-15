import { api } from '../api'
import type { ProcessingRun } from '../types'
import { useApiResource } from './useApiResource'

export interface ProcessingRuns {
  runs: ProcessingRun[]
  loading: boolean
  error: string | null
  reload: () => void
}

export function useProcessingRuns(onUnauthorized: () => void): ProcessingRuns {
  const resource = useApiResource(
    (signal) => api.listProcessingActivity({ signal }),
    [],
    { onUnauthorized },
  )

  return {
    runs: resource.data?.items ?? [],
    loading: resource.loading,
    error: resource.error,
    reload: resource.reload,
  }
}
