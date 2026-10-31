import { useState } from 'react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { Workspace, WorkspaceMember, WorkspaceRole } from '../types'
import { Dialog, DialogActions } from './Dialog'
import { RoleTiles } from './ui/RoleTiles'

interface ChangeMemberAccessDialogProps {
  open: boolean
  workspace: Workspace
  token: string
  member: WorkspaceMember
  isAdmin: boolean
  onUnauthorized: () => void
  onSaved: (member: WorkspaceMember) => void
  onClose: () => void
}

/**
 * Change one member's role with an explicit save.
 *
 * Nothing is written until Save is pressed, and the row the dialog came from
 * stays untouched until then. The last-owner rule is enforced by the API and
 * surfaces here as an inline error.
 */
export function ChangeMemberAccessDialog({
  open,
  workspace,
  token,
  member,
  isAdmin,
  onUnauthorized,
  onSaved,
  onClose,
}: ChangeMemberAccessDialogProps) {
  const [role, setRole] = useState<WorkspaceRole>(member.role)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const roles: WorkspaceRole[] = isAdmin
    ? ['owner', 'editor', 'viewer']
    : ['editor', 'viewer']

  async function handleSave() {
    if (role === member.role) return
    setSaving(true)
    setError(null)
    try {
      const updated = await api.setMemberRole(token, workspace.id, member.user_id, role, isAdmin)
      setSaving(false)
      onSaved(updated)
    } catch (caught) {
      if (isUnauthorized(caught)) {
        onUnauthorized()
        return
      }
      setError(getErrorMessage(caught))
      setSaving(false)
    }
  }

  return (
    <Dialog
      open={open}
      eyebrow={workspace.name}
      title="Change access"
      description={`Pick the new role for ${member.username}. The change applies once you save it.`}
      onClose={onClose}
    >
      <div className="px-5 py-5 sm:px-6">
        <RoleTiles
          label="Workspace role"
          labelId="change-access-role-label"
          value={role}
          onChange={setRole}
          roles={roles}
        />
        {error === null ? null : (
          <p className="error-note mt-5" role="alert">
            {error}
          </p>
        )}
      </div>
      <DialogActions>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={saving}
          onClick={onClose}
        >
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-primary"
          disabled={saving || role === member.role}
          onClick={handleSave}
        >
          {saving ? 'Saving…' : 'Save access'}
        </button>
      </DialogActions>
    </Dialog>
  )
}
