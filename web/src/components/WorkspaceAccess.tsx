import { CheckCircle2, CircleSlash2, FileUp, Sparkles, Trash2, Users } from 'lucide-react'
import type { Workspace } from '../types'

interface WorkspaceAccessProps {
  workspace: Workspace
  isAdmin: boolean
  canManage: boolean
  canDelete: boolean
}

/**
 * What the caller may do in this workspace, and why.
 *
 * Presentational on purpose: the answer is already on the workspace record,
 * and the panel only has to say it.
 */
export function WorkspaceAccess({
  workspace,
  isAdmin,
  canManage,
  canDelete,
}: WorkspaceAccessProps) {
  return (
    <>
      <div className="grid grid-cols-2 border-b border-line sm:grid-cols-4">
        <Capability
          icon={<FileUp aria-hidden="true" size={16} />}
          label="Upload"
          enabled={workspace.can_upload}
        />
        <Capability
          icon={<Sparkles aria-hidden="true" size={16} />}
          label="Context"
          enabled={workspace.can_search_context}
        />
        <Capability
          icon={<Users aria-hidden="true" size={16} />}
          label="Members"
          enabled={canManage}
        />
        <Capability
          icon={<Trash2 aria-hidden="true" size={16} />}
          label="Delete"
          enabled={canDelete}
        />
      </div>

      <section className="border-b border-line px-5 py-5 sm:px-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="eyebrow">Your access</p>
            <h3 className="mt-1 text-base font-semibold text-ink">
              {isAdmin
                ? 'Platform administrator'
                : (workspace.membership_role ?? 'Catalog only')}
            </h3>
          </div>
          {workspace.membership_role === null ? null : (
            <span className="badge">{workspace.membership_role}</span>
          )}
        </div>
        <p className="mt-2 text-sm leading-6 text-ink-muted">
          {isAdmin
            ? 'Administration permits membership changes, but does not grant document or context access without a workspace membership.'
            : workspace.membership_role === null
              ? 'You can discover this workspace, but its documents and context remain inaccessible.'
              : 'Capabilities are derived from your current workspace membership and update immediately when the policy changes.'}
        </p>
      </section>
    </>
  )
}

interface CapabilityProps {
  icon: React.ReactNode
  label: string
  enabled: boolean
}

function Capability({ icon, label, enabled }: CapabilityProps) {
  return (
    <div className="border-r border-line px-4 py-3.5 last:border-r-0">
      <div className={enabled ? 'text-accent-ink' : 'text-ink-faint'}>{icon}</div>
      <p className="mt-1.5 text-xs font-medium text-ink">{label}</p>
      <p className="mt-0.5 flex items-center gap-1 text-meta text-ink-muted">
        {enabled ? (
          <CheckCircle2 aria-hidden="true" size={11} />
        ) : (
          <CircleSlash2 aria-hidden="true" size={11} />
        )}
        {enabled ? 'Allowed' : 'Not allowed'}
      </p>
    </div>
  )
}
