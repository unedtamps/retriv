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
 * and the panel only has to say it. One line carries the role and its four
 * capabilities, because four boxes of the same green check said the same
 * thing at four times the height.
 */
export function WorkspaceAccess({
  workspace,
  isAdmin,
  canManage,
  canDelete,
}: WorkspaceAccessProps) {
  const role = isAdmin
    ? 'Platform administrator'
    : (workspace.membership_role ?? 'Catalog only')
  const capabilities: Array<{ icon: typeof FileUp; label: string; enabled: boolean }> = [
    { icon: FileUp, label: 'Upload', enabled: workspace.can_upload },
    { icon: Sparkles, label: 'Context', enabled: workspace.can_search_context },
    { icon: Users, label: 'Members', enabled: canManage },
    { icon: Trash2, label: 'Delete', enabled: canDelete },
  ]

  return (
    <section className="border-b border-line px-5 py-4 sm:px-6">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <p className="eyebrow shrink-0">Your access</p>
        <span className="badge shrink-0">{role}</span>
        <span className="hidden h-4 w-px bg-line sm:block" aria-hidden="true" />
        <ul className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
          {capabilities.map(({ icon: Icon, label, enabled }) => (
            <li
              key={label}
              className={`flex items-center gap-1.5 text-xs ${
                enabled ? 'text-ink' : 'text-ink-faint'
              }`}
              title={enabled ? `${label}: allowed` : `${label}: not allowed`}
            >
              <Icon aria-hidden="true" size={13} className={enabled ? 'text-success' : ''} />
              {label}
              {enabled ? (
                <CheckCircle2 aria-hidden="true" size={11} className="text-success" />
              ) : (
                <CircleSlash2 aria-hidden="true" size={11} />
              )}
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
