import { Layers3 } from 'lucide-react'

/**
 * The mark: a lime tile with the product's glyph.
 *
 * It appears in the rail, in the mobile bar and on the sign-in page, so it
 * is one component rather than three hand-written tiles.
 */
export function BrandMark({ size = 17 }: { size?: number }) {
  return (
    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-card bg-accent text-accent-ink">
      <Layers3 aria-hidden="true" size={size} strokeWidth={2.2} />
    </span>
  )
}

/** The mark with the product name beside it. */
export function BrandLockup({ subtitle = 'Control plane' }: { subtitle?: string }) {
  return (
    <div className="flex items-center gap-3">
      <BrandMark />
      <div className="min-w-0">
        <p className="text-sm font-semibold text-ink">Retriv</p>
        <p className="eyebrow">{subtitle}</p>
      </div>
    </div>
  )
}
