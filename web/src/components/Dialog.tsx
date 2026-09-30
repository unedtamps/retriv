import { useEffect, useId, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'

interface DialogProps {
  open: boolean
  title: string
  eyebrow?: string
  description?: string
  variant?: 'modal' | 'drawer'
  preventBackdropClose?: boolean
  children: ReactNode
  onClose: () => void
}

export function Dialog({
  open,
  title,
  eyebrow,
  description,
  variant = 'modal',
  preventBackdropClose = false,
  children,
  onClose,
}: DialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const descriptionId = useId()

  useEffect(() => {
    const dialog = dialogRef.current
    if (dialog === null) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])

  return (
    <dialog
      ref={dialogRef}
      className={variant === 'drawer' ? 'app-dialog app-drawer' : 'app-dialog'}
      aria-labelledby={titleId}
      aria-describedby={description === undefined ? undefined : descriptionId}
      onCancel={(event) => {
        if (preventBackdropClose) event.preventDefault()
      }}
      onClose={() => {
        if (open) onClose()
      }}
      onClick={(event) => {
        if (!preventBackdropClose && event.target === event.currentTarget) onClose()
      }}
    >
      <section className="dialog-surface" onClick={(event) => event.stopPropagation()}>
        <header className="flex items-start justify-between gap-5 border-b border-stone-200 px-5 py-5 sm:px-7">
          <div className="min-w-0">
            {eyebrow === undefined ? null : (
              <p className="mb-1 font-mono text-[0.68rem] font-semibold uppercase tracking-[0.18em] text-emerald-800">
                {eyebrow}
              </p>
            )}
            <h2 id={titleId} className="text-xl font-semibold tracking-[-0.02em] text-stone-950">
              {title}
            </h2>
            {description === undefined ? null : (
              <p id={descriptionId} className="mt-1.5 max-w-xl text-sm leading-6 text-stone-600">
                {description}
              </p>
            )}
          </div>
          <button
            type="button"
            className="icon-button shrink-0"
            aria-label={`Close ${title}`}
            onClick={onClose}
          >
            <X aria-hidden="true" size={18} strokeWidth={1.8} />
          </button>
        </header>
        {children}
      </section>
    </dialog>
  )
}

interface ConfirmDialogProps {
  open: boolean
  title: string
  description: string
  confirmLabel: string
  busy?: boolean
  destructive?: boolean
  onConfirm: () => void
  onClose: () => void
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  busy = false,
  destructive = false,
  onConfirm,
  onClose,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      title={title}
      description={description}
      onClose={onClose}
      preventBackdropClose={busy}
    >
      <div className="flex flex-col-reverse gap-3 px-5 py-5 sm:flex-row sm:justify-end sm:px-7">
        <button type="button" className="button-secondary" disabled={busy} onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className={destructive ? 'button-danger' : 'button-primary'}
          disabled={busy}
          onClick={onConfirm}
        >
          {busy ? 'Working…' : confirmLabel}
        </button>
      </div>
    </Dialog>
  )
}
