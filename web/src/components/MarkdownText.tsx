import { isValidElement, type ReactNode } from 'react'
import remarkGfmModule from 'remark-gfm'
import ReactMarkdown from 'react-markdown'

// The plugin ships as an ESM default export; depending on the transform in
// front of it the named shape may not survive, so unwrap it once here.
const remarkGfm =
  typeof remarkGfmModule === 'function'
    ? remarkGfmModule
    : (remarkGfmModule as unknown as { default: typeof remarkGfmModule }).default

/**
 * Assistant prose, rendered as markdown.
 *
 * The agent answers in markdown -- headings, lists, tables, code -- because a
 * paragraph about a report reads like a report. Raw HTML is deliberately not
 * enabled: the prose quotes document text, and document text is untrusted
 * input wearing a citation. `react-markdown` renders escaped text by default
 * and its URL transform drops `javascript:` links, which is the whole of the
 * security story and the reason this library and not an HTML-string one.
 *
 * Kept as its own module so the markdown pipeline stays out of the entry
 * bundle: the chart, spreadsheet and Word renderers set that precedent, and a
 * user who never opens the chat never parses prose.
 */
function textOf(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children)
  return ''
}

export function MarkdownText({ text }: { text: string }) {
  return (
    <div className="retriv-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props) => <a {...props} target="_blank" rel="noreferrer" />,
          td: ({ children, node: _node, ...props }) => {
            const label = textOf(children).trim().toUpperCase()
            const tone = label.startsWith('OVER')
              ? 'chat-status-over'
              : label === 'WITHIN'
                ? 'chat-status-within'
                : undefined
            return (
              <td {...props} className={tone}>
                {children}
              </td>
            )
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
