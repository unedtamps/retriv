import { useEffect, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'

interface RefreshButtonProps {
  label: string
  loading: boolean
  onRefresh: () => void
}

const MINIMUM_SPIN_MS = 700

export function RefreshButton({ label, loading, onRefresh }: RefreshButtonProps) {
  const [spinning, setSpinning] = useState(false)
  const startedAt = useRef(0)

  useEffect(() => {
    if (loading || !spinning) return
    const elapsed = performance.now() - startedAt.current
    const timer = window.setTimeout(
      () => setSpinning(false),
      Math.max(0, MINIMUM_SPIN_MS - elapsed),
    )
    return () => window.clearTimeout(timer)
  }, [loading, spinning])

  return (
    <button
      type="button"
      className="btn-icon h-8 w-8 border-transparent bg-transparent hover:border-line"
      aria-label={label}
      disabled={spinning}
      onClick={() => {
        startedAt.current = performance.now()
        setSpinning(true)
        onRefresh()
      }}
    >
      <RefreshCw aria-hidden="true" size={15} className={spinning ? 'animate-spin' : undefined} />
    </button>
  )
}
