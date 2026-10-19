import { useEffect, useId, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
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

  return createPortal(
    <dialog
      ref={dialogRef}
      className={variant === 'drawer' ? 'app-dialog app-drawer' : 'app-dialog'}
      aria-labelledby={titleId}
      aria-describedby={description === undefined ? undefined : descriptionId}
      onCancel={(event) => {
        if (preventBackdropClose) event.preventDefault()
      }}
      onClose={(event) => {
        // A dialog nested inside another one closes with it, and the close
        // event of the inner one passes through the outer one on the way
        // down the tree. Without this the drawer would close when a
        // confirmation inside it was dismissed.
        if (event.target !== dialogRef.current) return
        if (open) onClose()
      }}
      onClick={(event) => {
        if (!preventBackdropClose && event.target === event.currentTarget) onClose()
      }}
    >
      <section className="dialog-surface" onClick={(event) => event.stopPropagation()}>
        <header className="flex items-start justify-between gap-5 border-b border-line px-5 py-4 sm:px-6">
          <div className="min-w-0">
            {eyebrow === undefined ? null : <p className="eyebrow">{eyebrow}</p>}
            <h2 id={titleId} className="mt-1 text-lg font-semibold tracking-[-0.015em] text-ink">
              {title}
            </h2>
            {description === undefined ? null : (
              <p id={descriptionId} className="mt-1.5 max-w-xl text-sm leading-6 text-ink-muted">
                {description}
              </p>
            )}
          </div>
          <button
            type="button"
            className="btn-icon shrink-0"
            aria-label={`Close ${title}`}
            onClick={onClose}
          >
            <X aria-hidden="true" size={18} strokeWidth={1.8} />
          </button>
        </header>
        {children}
      </section>
    </dialog>,
    document.body,
  )
}

/**
 * The row of buttons at the foot of a dialog.
 *
 * Six dialogs had this row written out, each with its own gap and its own
 * border colour.
 */
export function DialogActions({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col-reverse gap-2 border-t border-line px-5 py-4 sm:flex-row sm:justify-end sm:px-6">
      {children}
    </div>
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
      <DialogActions>
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className={destructive ? 'btn btn-danger' : 'btn btn-primary'}
          disabled={busy}
          onClick={onConfirm}
        >
          {busy ? 'Working…' : confirmLabel}
        </button>
      </DialogActions>
    </Dialog>
  )
}
