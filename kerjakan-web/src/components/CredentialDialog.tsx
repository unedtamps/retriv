import { useState } from 'react'
import { Check, Copy, KeyRound } from 'lucide-react'
import { Dialog } from './Dialog'
import type { OneTimeCredential } from '../types'

interface CredentialDialogProps {
  credential: OneTimeCredential | null
  onClose: () => void
}

export function CredentialDialog({ credential, onClose }: CredentialDialogProps) {
  const [copied, setCopied] = useState(false)

  async function copyCredentials() {
    if (credential === null) return
    const value = `Username: ${credential.username}\nPassword: ${credential.password}`
    await navigator.clipboard.writeText(value)
    setCopied(true)
  }

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
        <div className="px-5 py-6 sm:px-7">
          <div className="mb-5 flex items-start gap-3 border border-amber-300 bg-amber-50 p-4 text-sm leading-6 text-amber-950">
            <KeyRound className="mt-0.5 shrink-0" aria-hidden="true" size={18} />
            <p>
              This is the only plaintext copy. Kerjakan stores only the password hash.
            </p>
          </div>
          <dl className="divide-y divide-stone-200 border-y border-stone-200 bg-stone-50">
            <div className="grid grid-cols-[7rem_1fr] gap-4 px-4 py-3">
              <dt className="text-sm text-stone-500">Username</dt>
              <dd className="font-mono text-sm font-semibold text-stone-950">
                {credential.username}
              </dd>
            </div>
            <div className="grid grid-cols-[7rem_1fr] gap-4 px-4 py-3">
              <dt className="text-sm text-stone-500">Password</dt>
              <dd className="break-all font-mono text-sm font-semibold text-stone-950">
                {credential.password}
              </dd>
            </div>
          </dl>
          <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
            <button type="button" className="button-secondary" onClick={onClose}>
              I have stored it safely
            </button>
            <button type="button" className="button-primary" onClick={copyCredentials}>
              {copied ? <Check aria-hidden="true" size={17} /> : <Copy aria-hidden="true" size={17} />}
              {copied ? 'Copied' : 'Copy credentials'}
            </button>
          </div>
        </div>
      )}
    </Dialog>
  )
}
