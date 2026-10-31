import { useEffect, useState } from 'react'
import { Check } from 'lucide-react'
import { Avatar } from './Avatar'
import { SearchField } from './SearchField'

export interface UserRosterEntry {
  id: string
  username: string
  role?: string
}

interface UserSearchRosterProps {
  id: string
  label: string
  search: string
  onSearchChange: (value: string) => void
  candidates: UserRosterEntry[]
  searching: boolean
  selectedId: string
  onSelect: (userId: string) => void
  emptyMessage: string
  total: number
}

/**
 * The shared user picker: a filter-as-you-type field above a roster of
 * selectable rows. The roster owns no fetching — the caller keeps the search
 * state, runs the debounced directory query, and applies its own eligibility
 * rules to what it passes in as candidates.
 */
export function UserSearchRoster({
  id,
  label,
  search,
  onSearchChange,
  candidates,
  searching,
  selectedId,
  onSelect,
  emptyMessage,
  total,
}: UserSearchRosterProps) {
  const [slowSearch, setSlowSearch] = useState(false)

  useEffect(() => {
    const timer = window.setTimeout(() => setSlowSearch(searching), searching ? 500 : 0)
    return () => window.clearTimeout(timer)
  }, [searching])

  // The entrance animation belongs to the first list that appears. Replaying
  // it on every keystroke is what made typing feel like the whole dialog was
  // re-rendering, so it runs once and then the rows simply swap.
  const [entering, setEntering] = useState(true)
  useEffect(() => {
    if (!entering || candidates.length === 0) return
    const timer = window.setTimeout(() => setEntering(false), 400)
    return () => window.clearTimeout(timer)
  }, [entering, candidates.length])

  const meta =
    candidates.length === total
      ? `${candidates.length} ${candidates.length === 1 ? 'user' : 'users'}`
      : `${candidates.length} of ${total} users`
  return (
    <div className="space-y-3">
      <div className="relative">
        <SearchField
          id={id}
          label={label}
          value={search}
          onChange={onSearchChange}
          placeholder="Filter users"
          className="[&_input]:pr-24"
        />
        <span className="pointer-events-none absolute right-3.5 top-1/2 -translate-y-1/2 font-mono text-[0.65rem] text-ink-faint">
          {searching && slowSearch ? 'Searching…' : meta}
        </span>
      </div>

      <div className="h-56 overflow-hidden rounded-card border border-line">
        {candidates.length === 0 && !searching ? (
          <p className="px-3.5 py-4 text-sm leading-6 text-ink-faint">{emptyMessage}</p>
        ) : (
          <ul className="h-full divide-y divide-line overflow-y-auto">
            {candidates.map((user, index) => {
              const selected = user.id === selectedId
              return (
                <li
                  key={user.id}
                  className={entering ? 'roster-enter' : undefined}
                  style={entering ? { animationDelay: `${Math.min(index, 8) * 24}ms` } : undefined}
                >
                  <button
                    type="button"
                    aria-pressed={selected}
                    onClick={() => onSelect(user.id)}
                    className={`flex w-full items-center gap-3 px-3.5 py-2.5 text-left transition-colors duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-ink ${
                      selected ? 'bg-accent-soft' : 'hover:bg-sunken'
                    }`}
                  >
                    <Avatar name={user.username} seed={user.id} size="xs" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-ink">
                        {user.username}
                      </span>
                      <span className="mt-0.5 block font-mono text-meta text-ink-faint">
                        {user.id.slice(0, 8)}
                      </span>
                    </span>
                    {user.role === undefined ? null : (
                      <span className="badge shrink-0">{user.role}</span>
                    )}
                    {selected ? (
                      <span className="flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-full bg-accent">
                        <Check
                          aria-hidden="true"
                          size={11}
                          strokeWidth={3}
                          className="text-white"
                        />
                      </span>
                    ) : (
                      <span
                        aria-hidden="true"
                        className="h-[18px] w-[18px] shrink-0 rounded-full border border-line-strong"
                      />
                    )}
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
