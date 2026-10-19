import { useEffect, useState, type ReactNode } from 'react'
import { Check, Copy } from 'lucide-react'

interface EmptyStateProps {
  icon?: ReactNode
  title: string
  description?: string
  action?: ReactNode
}

/** The nothing-here panel: an icon, a title, a sentence, and what to do next. */
export function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="px-5 py-12 text-center">
      {icon === undefined ? null : (
        <span className="mx-auto flex h-10 w-10 items-center justify-center rounded-card border border-line bg-sunken text-ink-faint">
          {icon}
        </span>
      )}
      <h3 className="mt-4 text-sm font-semibold text-ink">{title}</h3>
      {description === undefined ? null : (
        <p className="mx-auto mt-1.5 max-w-md text-sm leading-6 text-ink-muted">{description}</p>
      )}
      {action === undefined ? null : <div className="mt-5">{action}</div>}
    </div>
  )
}

interface CopyButtonProps {
  value: string
  label?: string
  copiedLabel?: string
}

/**
 * Copies a secret to the clipboard and says so for a moment.
 *
 * The one-time credential and the API key each had their own copy button,
 * each with its own copied state and its own clipboard call.
 */
export function CopyButton({
  value,
  label = 'Copy',
  copiedLabel = 'Copied',
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), 2000)
    return () => window.clearTimeout(timer)
  }, [copied])

  return (
    <button
      type="button"
      className="btn btn-secondary"
      onClick={async () => {
        await navigator.clipboard.writeText(value)
        setCopied(true)
      }}
    >
      {copied ? <Check aria-hidden="true" size={16} /> : <Copy aria-hidden="true" size={16} />}
      {copied ? copiedLabel : label}
    </button>
  )
}

/**
 * The panel that warns a secret is shown once.
 *
 * It was written out twice: once in the API key reveal and once in the
 * credential reveal, in two slightly different shades of amber.
 */
export function SecretWarning({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-start gap-3 rounded-card border border-warn/30 bg-warn-soft px-4 py-3 text-sm leading-6 text-warn">
      <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-warn" aria-hidden="true" />
      <div className="min-w-0">{children}</div>
    </div>
  )
}
