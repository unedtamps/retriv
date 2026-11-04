import type { DashboardView, Workspace } from '../types'

const MEMBER_VIEWS: DashboardView[] = ['chat', 'overview', 'documents', 'members', 'api-keys']
const ADMIN_ONLY_VIEWS: DashboardView[] = ['users', 'agent-model']
const ADMIN_MODE_VIEWS: DashboardView[] = ['admin-overview', 'users', 'agent-model']

/** Views that describe one selected workspace and survive a workspace switch. */
export const WORKSPACE_VIEWS: DashboardView[] = ['overview', 'documents', 'members']

/**
 * Where a dashboard deep link points.
 *
 * The dashboard view and the selected workspace live in the URL so a browser
 * refresh or a shared link lands on the same spot instead of the default
 * landing page. Reading is defensive: anything the current user could not
 * have navigated to themselves is ignored.
 */
export function readDashboardView(
  search: string,
  isAdmin: boolean,
  adminMode: boolean,
): DashboardView | null {
  const view = new URLSearchParams(search).get('view') as DashboardView | null
  if (view === null) return null
  const allowed = adminMode
    ? ADMIN_MODE_VIEWS
    : isAdmin
      ? [...MEMBER_VIEWS, ...ADMIN_ONLY_VIEWS]
      : MEMBER_VIEWS
  return allowed.includes(view) ? view : null
}

export function readDashboardWorkspaceId(
  search: string,
  workspaces: Workspace[],
): string | null {
  const workspaceId = new URLSearchParams(search).get('ws')
  if (workspaceId === null) return null
  return workspaces.some((workspace) => workspace.id === workspaceId) ? workspaceId : null
}

export function dashboardHref(
  view: DashboardView,
  workspaceId: string | null,
  adminMode: boolean,
): string {
  const params = new URLSearchParams({ view })
  if (!adminMode && workspaceId !== null) params.set('ws', workspaceId)
  return `/dashboard?${params.toString()}`
}
