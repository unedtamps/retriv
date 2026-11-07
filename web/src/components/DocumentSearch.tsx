import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { FileText, Search } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { DocumentSearchHit } from '../types'
import { documentStatusTone, runStatusLabel, STATUS_TONE_CLASSES } from '../lib/visuals'

interface DocumentSearchProps {
  inputRef?: RefObject<HTMLInputElement | null>
  value: string
  onChange: (value: string) => void
  onOpenDocument: (hit: DocumentSearchHit) => void
  onUnauthorized: () => void
}

/**
 * The top-bar search: one field that filters the open table and searches
 * every workspace the caller can read.
 *
 * Two jobs in one box on purpose. The typed text still narrows the current
 * workspace's table, and the same text is asked of the API across all
 * readable workspaces, so a document in another workspace is one click away
 * instead of invisible. The dropdown is a portal measured off the field, like
 * the other popovers in the header, because the bar clips its own overflow.
 */
export function DocumentSearch({
  value,
  onChange,
  onOpenDocument,
  onUnauthorized,
  inputRef,
}: DocumentSearchProps) {
  const [debounced, setDebounced] = useState('')
  const [loadedQuery, setLoadedQuery] = useState<string | null>(null)
  const [hits, setHits] = useState<DocumentSearchHit[]>([])
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const [anchor, setAnchor] = useState<{ top: number; left: number; width: number } | null>(null)

  const containerRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value.trim()), 420)
    return () => window.clearTimeout(timer)
  }, [value])

  useEffect(() => {
    if (debounced.length === 0) return
    const controller = new AbortController()
    api
      .searchDocuments({ query: debounced, limit: 20, signal: controller.signal })
      .then((result) => {
        setHits(result.items)
        setTotal(result.total)
        setError(null)
        setLoadedQuery(debounced)
        setActiveIndex(-1)
      })
      .catch((caught: unknown) => {
        if (caught instanceof Error && caught.name === 'AbortError') return
        setLoadedQuery(debounced)
        if (isUnauthorized(caught)) onUnauthorized()
        else setError(getErrorMessage(caught))
      })
    return () => controller.abort()
  }, [debounced, onUnauthorized])

  const visibleHits = debounced.length === 0 ? [] : hits
  const stale = debounced.length > 0 && loadedQuery !== debounced

  // "Searching…" only after the wait is long enough to notice. A local API
  // answers in tens of milliseconds, and flashing the label in and out makes
  // the panel jump for no information in return.
  const [slow, setSlow] = useState(false)
  useEffect(() => {
    const timer = window.setTimeout(() => setSlow(stale), stale ? 600 : 0)
    return () => window.clearTimeout(timer)
  }, [stale])
  const showSearching = stale && slow

  function openPanel() {
    const rect = containerRef.current?.getBoundingClientRect()
    if (rect) setAnchor({ top: rect.bottom + 6, left: rect.left, width: rect.width })
    setOpen(true)
  }

  useEffect(() => {
    if (!open) return

    function closeOnEscape(event: globalThis.KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    function closeOnPointerDown(event: PointerEvent) {
      const target = event.target as Node
      if (panelRef.current?.contains(target) || containerRef.current?.contains(target)) return
      setOpen(false)
    }

    window.addEventListener('keydown', closeOnEscape)
    document.addEventListener('pointerdown', closeOnPointerDown)
    return () => {
      window.removeEventListener('keydown', closeOnEscape)
      document.removeEventListener('pointerdown', closeOnPointerDown)
    }
  }, [open])

  const showPanel = open && value.trim().length > 0
  const optionId = (index: number) => `document-search-option-${index}`

  const state = useMemo(() => {
    if (visibleHits.length > 0) return 'results' as const
    if (showSearching) return 'searching' as const
    if (error !== null) return 'error' as const
    return 'empty' as const
  }, [showSearching, error, visibleHits.length])

  function choose(hit: DocumentSearchHit) {
    setOpen(false)
    setActiveIndex(-1)
    onOpenDocument(hit)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      if (!showPanel) openPanel()
      setActiveIndex((index) => Math.min(index + 1, visibleHits.length - 1))
      return
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((index) => Math.max(index - 1, -1))
      return
    }
    if (event.key === 'Enter' && showPanel && activeIndex >= 0) {
      event.preventDefault()
      const hit = visibleHits[activeIndex]
      if (hit !== undefined) choose(hit)
    }
  }

  return (
    <div ref={containerRef} className="relative hidden w-64 md:block xl:w-72">
      <Search
        aria-hidden="true"
        className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint"
        size={15}
      />
      <label className="sr-only" htmlFor="global-search">
        Search documents
      </label>
      <input
        ref={inputRef}
        id="global-search"
        type="search"
        role="combobox"
        className="field h-9 py-1.5 pl-8 pr-12 text-xs"
        value={value}
        placeholder="Search documents…"
        autoComplete="off"
        aria-expanded={showPanel}
        aria-controls="document-search-panel"
        aria-autocomplete="list"
        aria-activedescendant={activeIndex >= 0 ? optionId(activeIndex) : undefined}
        onFocus={() => {
          if (value.trim().length > 0) openPanel()
        }}
        onChange={(event) => {
          onChange(event.target.value)
          openPanel()
        }}
        onKeyDown={handleKeyDown}
      />
      <kbd className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 rounded border border-line bg-sunken px-1.5 py-0.5 font-mono text-[0.6rem] text-ink-faint">
        ⌘K
      </kbd>

      {showPanel
        ? createPortal(
            <div
              ref={panelRef}
              id="document-search-panel"
              role="listbox"
              aria-label="Document search results"
              style={anchor === null ? undefined : { top: anchor.top, left: anchor.left, width: Math.max(anchor.width, 320) }}
              className="popover-enter fixed z-[60] overflow-hidden rounded-card border border-line bg-surface shadow-raised"
            >
              {state === 'searching' ? (
                <p className="px-3.5 py-3 text-xs text-ink-muted" role="status">
                  Searching…
                </p>
              ) : state === 'error' ? (
                <p className="error-note m-2.5" role="alert">
                  {error}
                </p>
              ) : state === 'empty' ? (
                <p className="px-3.5 py-3 text-xs text-ink-muted">
                  No document matches “{debounced}” in your workspaces.
                </p>
              ) : (
                <>
                  <ul className="max-h-80 overflow-y-auto overscroll-contain">
                    {visibleHits.map((hit, index) => {
                      const active = index === activeIndex
                      return (
                        <li key={`${hit.workspace_id}-${hit.document_id}`}>
                          <button
                            type="button"
                            id={optionId(index)}
                            role="option"
                            aria-selected={active}
                            className={`roster-enter flex w-full items-start gap-2.5 border-b border-line px-3 py-2.5 text-left last:border-b-0 ${
                              active ? 'bg-sunken' : 'hover:bg-sunken'
                            }`}
                            style={{ animationDelay: `${Math.min(index, 8) * 24}ms` }}
                            onClick={() => choose(hit)}
                            onPointerEnter={() => setActiveIndex(index)}
                          >
                            <FileText
                              aria-hidden="true"
                              className="mt-0.5 shrink-0 text-ink-faint"
                              size={14}
                            />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-xs font-medium text-ink">
                                {hit.filename}
                              </span>
                              <span className="mt-1 flex items-center gap-1.5">
                                <span className="truncate font-mono text-[0.6rem] text-ink-faint">
                                  {hit.workspace_name}
                                </span>
                                <span
                                  className={`stamp shrink-0 ${STATUS_TONE_CLASSES[documentStatusTone(hit.status)]}`}
                                >
                                  {runStatusLabel(hit.status)}
                                </span>
                              </span>
                            </span>
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                  <p className="border-t border-line px-3 py-1.5 text-right font-mono text-[0.6rem] text-ink-faint">
                    {visibleHits.length} of {total}
                  </p>
                </>
              )}
            </div>,
            document.body,
          )
        : null}
    </div>
  )
}
