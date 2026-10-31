import { useEffect, useRef, useState } from 'react'
import { Check, Monitor, Moon, Sun } from 'lucide-react'
import type { ThemePreference } from '../hooks/useTheme'

interface ThemeMenuProps {
  preference: ThemePreference
  resolved: 'light' | 'dark'
  onSelect: (preference: ThemePreference) => void
}

const OPTIONS: Array<{ id: ThemePreference; label: string; icon: typeof Sun }> = [
  { id: 'system', label: 'System', icon: Monitor },
  { id: 'light', label: 'Light', icon: Sun },
  { id: 'dark', label: 'Dark', icon: Moon },
]

/**
 * The theme control: one icon that shows what is in effect, and a menu that
 * names the three choices.
 *
 * It sits in the sidebar footer because the theme is a personal setting, next
 * to the sign-out button rather than among the workspace navigation.
 */
export function ThemeMenu({ preference, resolved, onSelect }: ThemeMenuProps) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const TriggerIcon = preference === 'system' ? Monitor : resolved === 'dark' ? Moon : Sun

  useEffect(() => {
    if (!open) return

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    function closeOnPointerDown(event: PointerEvent) {
      if (containerRef.current?.contains(event.target as Node)) return
      setOpen(false)
    }

    window.addEventListener('keydown', closeOnEscape)
    document.addEventListener('pointerdown', closeOnPointerDown)
    return () => {
      window.removeEventListener('keydown', closeOnEscape)
      document.removeEventListener('pointerdown', closeOnPointerDown)
    }
  }, [open])

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        className="btn-icon h-8 w-8 border-transparent bg-transparent"
        aria-label={`Theme: ${preference === 'system' ? `system (${resolved})` : preference}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <TriggerIcon aria-hidden="true" size={15} />
      </button>

      {open ? (
        <div
          role="menu"
          aria-label="Theme"
          className="popover-enter absolute bottom-[calc(100%+6px)] right-0 z-50 w-40 overflow-hidden rounded-control border border-line bg-surface py-1 shadow-raised"
        >
          {OPTIONS.map(({ id, label, icon: Icon }) => {
            const active = preference === id
            return (
              <button
                key={id}
                type="button"
                role="menuitemradio"
                aria-checked={active}
                className={`flex w-full items-center gap-2.5 px-2.5 py-2 text-left text-sm transition-colors hover:bg-sunken ${
                  active ? 'text-accent-ink' : 'text-ink-muted'
                }`}
                onClick={() => {
                  setOpen(false)
                  onSelect(id)
                }}
              >
                <Icon aria-hidden="true" size={14} />
                <span className="min-w-0 flex-1">{label}</span>
                {active ? <Check aria-hidden="true" size={13} /> : null}
              </button>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}
