import { initials, workspaceAccent } from '../../lib/visuals'

interface AvatarProps {
  name: string
  seed?: string
  size?: 'xs' | 'sm' | 'md' | 'lg'
  className?: string
}

const SIZE_CLASSES = {
  xs: 'h-6 w-6 text-[0.6rem]',
  sm: 'h-8 w-8 text-[0.68rem]',
  md: 'h-9 w-9 text-xs',
  lg: 'h-11 w-11 text-sm',
}

export function Avatar({ name, seed, size = 'sm', className = '' }: AvatarProps) {
  const accent = workspaceAccent(seed ?? name)
  return (
    <span
      aria-hidden="true"
      className={`flex shrink-0 items-center justify-center rounded-stamp border font-mono font-semibold tracking-wide ${accent.chip} ${SIZE_CLASSES[size]} ${className}`}
    >
      {initials(name)}
    </span>
  )
}
