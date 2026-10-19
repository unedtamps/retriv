import { Dialog, DialogActions } from './Dialog'
import type { OneTimeCredential } from '../types'
import { CopyButton, SecretWarning } from './ui/Feedback'

interface CredentialDialogProps {
  credential: OneTimeCredential | null
  onClose: () => void
}

export function CredentialDialog({ credential, onClose }: CredentialDialogProps) {
  return (
    <Dialog
      open={credential !== null}
      title={credential?.reason === 'reset' ? 'New password generated' : 'User created'}
      eyebrow="One-time display"
      description="Share these credentials through a trusted channel. The password cannot be retrieved after this window closes."
      preventBackdropClose
      onClose={onClose}
    >
      {credential === null ? null : (
        <div className="px-5 py-5 sm:px-6">
          <SecretWarning>
            This is the only plaintext copy. Retriv stores only the password hash.
          </SecretWarning>
          <dl className="mt-4 divide-y divide-line rounded-card border border-line">
            <div className="grid grid-cols-[6rem_1fr] gap-4 px-4 py-2.5">
              <dt className="text-sm text-ink-muted">Username</dt>
              <dd className="font-mono text-sm font-semibold text-ink">{credential.username}</dd>
            </div>
            <div className="grid grid-cols-[6rem_1fr] gap-4 px-4 py-2.5">
              <dt className="text-sm text-ink-muted">Password</dt>
              <dd className="break-all font-mono text-sm font-semibold text-ink">
                {credential.password}
              </dd>
            </div>
          </dl>
          <DialogActions>
            <button type="button" className="btn btn-secondary" onClick={onClose}>
              I have stored it safely
            </button>
            <CopyButton
              value={`Username: ${credential.username}\nPassword: ${credential.password}`}
              label="Copy credentials"
            />
          </DialogActions>
        </div>
      )}
    </Dialog>
  )
}
