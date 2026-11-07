import { Check } from 'lucide-react'
import type { WorkspaceRole } from '../../types'

interface RoleTilesProps {
  label: string
  labelId?: string
  value: WorkspaceRole
  onChange: (role: WorkspaceRole) => void
  roles: WorkspaceRole[]
}

/**
 * The shared role picker: one tile per role, selected by click, saved by the
 * caller. The tiles only choose — every consumer decides when (or whether)
 * the change is committed, so no tile ever writes to the API itself.
 */
export function RoleTiles({ label, labelId, value, onChange, roles }: RoleTilesProps) {
  return (
    <div>
      <p className="label" id={labelId}>
        {label}
      </p>
      <div
        className="mt-2 grid grid-cols-3 gap-3"
        role="radiogroup"
        aria-labelledby={labelId}
      >
        {roles.map((choice) => {
          const active = value === choice
          return (
            <button
              key={choice}
              type="button"
              role="radio"
              aria-checked={active}
              className={`flex h-11 items-center justify-between gap-2 rounded-control border px-3.5 font-mono text-[11px] font-medium uppercase tracking-[0.08em] transition-colors duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${
                active
                  ? 'border-ink bg-sunken text-ink shadow-[inset_2px_0_0_var(--color-accent)]'
                  : 'border-line bg-surface text-ink-muted hover:border-line-strong'
              }`}
              onClick={() => onChange(choice)}
            >
              {choice}
              {active ? <Check aria-hidden="true" size={14} /> : null}
            </button>
          )
        })}
      </div>
    </div>
  )
}
