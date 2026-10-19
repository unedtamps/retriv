import { Search } from 'lucide-react'

interface SearchFieldProps {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  className?: string
}

/**
 * A search box with the icon in it.
 *
 * Four screens had this written out, each with its own wrapper div, its own
 * icon class and its own padding arithmetic on the input.
 */
export function SearchField({
  id,
  label,
  value,
  onChange,
  placeholder,
  className = '',
}: SearchFieldProps) {
  return (
    <div className={`relative ${className}`}>
      <Search
        aria-hidden="true"
        className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint"
        size={16}
      />
      <label className="sr-only" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        type="search"
        className="field pl-9"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  )
}
