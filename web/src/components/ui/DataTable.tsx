import type { ReactNode } from 'react'

export interface DataColumn<T> {
  key: string
  header: string
  render: (row: T) => ReactNode
  align?: 'left' | 'right'
}

interface DataTableProps<T> {
  caption: string
  columns: DataColumn<T>[]
  rows: T[]
  rowKey: (row: T) => string
  empty?: ReactNode
}

/**
 * A table that becomes a list of cards on a narrow screen.
 *
 * Two screens had this written out by hand, and both of them made the table
 * scroll sideways on a phone instead: a `min-w-[42rem]` table inside an
 * `overflow-x-auto` is a table nobody can read. Here the first column is the
 * card's heading and the rest become label and value pairs.
 */
export function DataTable<T>({ caption, columns, rows, rowKey, empty }: DataTableProps<T>) {
  if (rows.length === 0) {
    return <>{empty ?? null}</>
  }

  return (
    <>
      <div className="hidden sm:block">
        <table className="table">
          <caption className="sr-only">{caption}</caption>
          <thead>
            <tr>
              {columns.map((column) => (
                <th
                  key={column.key}
                  scope="col"
                  className={column.align === 'right' ? 'text-right' : undefined}
                >
                  {column.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={rowKey(row)}>
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className={column.align === 'right' ? 'text-right' : undefined}
                  >
                    {column.render(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ul className="divide-y divide-line sm:hidden">
        {rows.map((row) => (
          <li key={rowKey(row)} className="px-5 py-4">
            {columns.map((column, position) =>
              position === 0 ? (
                <div key={column.key} className="text-sm font-medium text-ink">
                  {column.render(row)}
                </div>
              ) : (
                <div
                  key={column.key}
                  className="mt-2 flex items-baseline justify-between gap-4"
                >
                  <span className="eyebrow">{column.header}</span>
                  <span className="min-w-0 text-right text-sm text-ink-muted">
                    {column.render(row)}
                  </span>
                </div>
              ),
            )}
          </li>
        ))}
      </ul>
    </>
  )
}
