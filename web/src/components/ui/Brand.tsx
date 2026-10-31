const MARK_PATH =
  'M20.6 5.9H12.85c-.31 0-.6-.13-.8-.37L10.66 3.9A1.55 1.55 0 0 0 9.47 3.35H3.4A1.65 1.65 0 0 0 1.75 5v13.35A1.65 1.65 0 0 0 3.4 20h17.2a1.65 1.65 0 0 0 1.65-1.65V7.55A1.65 1.65 0 0 0 20.6 5.9ZM12 8.5c.26 0 .51.1.69.29l2.93 3.08c.5.53.13 1.41-.59 1.41h-1.62v3.1c0 .59-.48 1.07-1.07 1.07h-.68c-.59 0-1.07-.48-1.07-1.07v-3.1H8.97c-.72 0-1.09-.88-.59-1.41l2.93-3.08c.18-.19.43-.29.69-.29Z'

/**
 * The mark: a navy tile with the folder-and-arrow glyph.
 *
 * The glyph is the chosen brand mark, drawn as one path with the arrow
 * knocked out, so it stays crisp from a 16 pixel favicon to the sign-in
 * tile. It appears in the rail, in the mobile bar and on the sign-in page,
 * so it is one component rather than three hand-written tiles.
 */
export function BrandMark({ size = 17 }: { size?: number }) {
  return (
    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-card bg-accent text-white">
      <svg aria-hidden="true" viewBox="0 0 24 24" width={size} height={size} fill="currentColor">
        <path fillRule="evenodd" d={MARK_PATH} />
      </svg>
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
