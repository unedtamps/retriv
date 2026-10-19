import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Bell } from 'lucide-react'
import type { Workspace } from '../types'
import { NotificationsPanel } from './NotificationsPanel'
import { useProcessingRuns } from '../hooks/useProcessingRuns'

interface ProcessingNotificationsProps {
  token: string
  workspaces: Workspace[]
  onUnauthorized: () => void
  onOpenChange: (open: boolean) => void
}

/**
 * The bell, and the panel it opens.
 *
 * The button and the panel are separate because the button is in the header
 * and the panel is a portal over the page, and because the panel has its own
 * focus management.
 */
export function ProcessingNotifications({
  token,
  workspaces,
  onUnauthorized,
  onOpenChange,
}: ProcessingNotificationsProps) {
  const [open, setOpen] = useState(false)
  const bellButtonRef = useRef<HTMLButtonElement>(null)
  const runs = useProcessingRuns(token, onUnauthorized)

  useEffect(() => {
    if (!open) return
    const bellButton = bellButtonRef.current
    return () => {
      window.requestAnimationFrame(() => bellButton?.focus())
    }
  }, [open])

  function changeOpen(nextOpen: boolean) {
    setOpen(nextOpen)
    onOpenChange(nextOpen)
  }

  const notificationCount = new Set([
    ...runs.recentRuns.map((run) => run.id),
    ...(runs.activeRun === null ? [] : [runs.activeRun.id]),
  ]).size

  return (
    <>
      <button
        ref={bellButtonRef}
        type="button"
        className="btn-icon relative"
        aria-label={
          notificationCount === 0
            ? 'Open processing notifications'
            : `Open processing notifications, ${notificationCount} recent runs`
        }
        aria-controls="processing-notifications-panel"
        aria-expanded={open}
        onClick={() => changeOpen(true)}
      >
        <Bell aria-hidden="true" size={18} />
        {notificationCount === 0 ? null : (
          <span
            aria-hidden="true"
            className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-accent-ink px-1 font-mono text-[0.58rem] leading-none text-accent"
          >
            {notificationCount > 99 ? '99+' : notificationCount}
          </span>
        )}
      </button>
      {open
        ? createPortal(
            <NotificationsPanel
              activeRun={runs.activeRun}
              recentRuns={runs.recentRuns}
              loading={runs.loading}
              error={runs.error}
              workspaces={workspaces}
              onRefresh={runs.reload}
              onClose={() => changeOpen(false)}
            />,
            document.body,
          )
        : null}
    </>
  )
}
