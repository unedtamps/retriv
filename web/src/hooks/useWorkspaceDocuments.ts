import { api } from '../api'
import type { DocumentUpload, Workspace } from '../types'
import { useApiResource } from './useApiResource'

export interface WorkspaceDocumentsData {
  items: DocumentUpload[]
  total: number
  offset: number
  limit: number
  complete: boolean
}

const PAGE_SIZE = 100
const MAX_PAGES = 20

/**
 * Every document the dashboard can show for one workspace.
 *
 * The API pages at 100 records. A workspace overview needs ready/processing
 * counts and storage totals, so the loader walks the remaining pages instead
 * of quietly deriving those numbers from the first 100 rows.
 */
export function useWorkspaceDocuments(
  workspace: Workspace | null,
  onUnauthorized: () => void,
) {
  return useApiResource<WorkspaceDocumentsData>(
    async (signal) => {
      if (workspace === null || workspace.membership_role === null) {
        return { items: [], total: 0, offset: 0, limit: PAGE_SIZE, complete: true }
      }

      const first = await api.listDocuments(workspace.id, {
        offset: 0,
        limit: PAGE_SIZE,
        signal,
      })
      const items = [...first.items]
      const pageCount = Math.min(Math.ceil(first.total / PAGE_SIZE), MAX_PAGES)
      const offsets: number[] = []
      for (let page = 1; page < pageCount; page += 1) {
        offsets.push(page * PAGE_SIZE)
      }
      if (offsets.length > 0) {
        const pages = await Promise.all(
          offsets.map((offset) =>
            api.listDocuments(workspace.id, {
              offset,
              limit: PAGE_SIZE,
              signal,
            }),
          ),
        )
        for (const page of pages) items.push(...page.items)
      }

      return {
        items,
        total: first.total,
        offset: 0,
        limit: PAGE_SIZE,
        complete: items.length >= first.total,
      }
    },
    [workspace?.id, workspace?.membership_role],
    { onUnauthorized },
  )
}
