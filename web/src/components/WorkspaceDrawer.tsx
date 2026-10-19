import { CircleSlash2 } from 'lucide-react'
import type { Workspace } from '../types'
import { Dialog } from './Dialog'
import { WorkspaceAccess } from './WorkspaceAccess'
import { WorkspaceDocuments } from './WorkspaceDocuments'
import { WorkspaceMembers } from './WorkspaceMembers'

interface WorkspaceDrawerProps {
  workspace: Workspace
  token: string
  isAdmin: boolean
  onChanged: () => Promise<void>
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onClose: () => void
}

/**
 * One workspace, opened beside the catalog.
 *
 * This used to be a 512-line file holding four concerns: what the caller may
 * do here, which documents are here, and who is in the workspace. Each is now
 * a component that owns its own data, and this is the frame around them.
 */
export function WorkspaceDrawer({
  workspace,
  token,
  isAdmin,
  onChanged,
  onUnauthorized,
  onNotice,
  onClose,
}: WorkspaceDrawerProps) {
  const canManage = isAdmin || workspace.can_manage_members
  // Membership role only. The API resolves document permissions through
  // the caller's membership with no platform-admin bypass, so adding
  // isAdmin here would render a button that answers 404.
  const canDelete = workspace.can_delete

  return (
    <Dialog
      open
      variant="drawer"
      title={workspace.name}
      eyebrow={workspace.slug}
      description={workspace.description ?? 'No workspace description has been provided.'}
      onClose={onClose}
    >
      <div className="drawer-scroll">
        <WorkspaceAccess
          workspace={workspace}
          isAdmin={isAdmin}
          canManage={canManage}
          canDelete={canDelete}
        />

        <WorkspaceDocuments
          workspace={workspace}
          token={token}
          canDelete={canDelete}
          onChanged={onChanged}
          onUnauthorized={onUnauthorized}
          onNotice={onNotice}
        />

        {canManage ? (
          <WorkspaceMembers
            workspace={workspace}
            token={token}
            isAdmin={isAdmin}
            onChanged={onChanged}
            onUnauthorized={onUnauthorized}
            onNotice={onNotice}
          />
        ) : (
          <section className="px-5 py-8 sm:px-6">
            <div className="rounded-card border border-dashed border-line-strong bg-sunken px-5 py-8 text-center">
              <CircleSlash2 className="mx-auto text-ink-faint" aria-hidden="true" size={24} />
              <h3 className="mt-3 text-sm font-semibold text-ink">
                Membership management unavailable
              </h3>
              <p className="mx-auto mt-1.5 max-w-sm text-sm leading-6 text-ink-muted">
                Only workspace owners and platform administrators can view or change this
                access policy.
              </p>
            </div>
          </section>
        )}
      </div>
    </Dialog>
  )
}
