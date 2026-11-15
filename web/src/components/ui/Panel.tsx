import type { ReactNode } from 'react'

export function Panel({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}>{children}</section>
}

interface PanelHeaderProps {
  title: string
  eyebrow?: string
  description?: ReactNode
  actions?: ReactNode
  id?: string
}

export function PanelHeader({
  title,
  eyebrow,
  description,
  actions,
  id,
}: PanelHeaderProps) {
  return (
    <header className="flex flex-col gap-3 border-b border-line px-5 py-4 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        {eyebrow === undefined ? null : <p className="eyebrow">{eyebrow}</p>}
        <h2 id={id} className="mt-1 text-lg font-semibold tracking-[-0.015em] text-ink">
          {title}
        </h2>
        {description === undefined ? null : (
          <div className="mt-1.5 max-w-2xl text-sm leading-6 text-ink-muted">{description}</div>
        )}
      </div>
      {actions === undefined ? null : (
        <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>
      )}
    </header>
  )
}
