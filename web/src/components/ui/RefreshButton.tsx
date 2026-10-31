import { RefreshCw } from 'lucide-react'

interface RefreshButtonProps {
  label: string
  loading: boolean
  onRefresh: () => void
}

/**
 * A quiet icon that refetches one table's data.
 *
 * It never reloads the page — the caller hands in the resource's own reload,
 * so pressing it costs exactly one request for the list it belongs to.
 */
export function RefreshButton({ label, loading, onRefresh }: RefreshButtonProps) {
  return (
    <button
      type="button"
      className="btn-icon h-8 w-8 border-transparent bg-transparent hover:border-line"
      aria-label={label}
      disabled={loading}
      onClick={onRefresh}
    >
      <RefreshCw aria-hidden="true" size={15} className={loading ? 'animate-spin' : undefined} />
    </button>
  )
}
