const ROLES: Array<{ role: string; grants: string }> = [
  {
    role: 'OWNER',
    grants: 'Upload and delete documents, manage members and roles, chat with full corpus scope.',
  },
  {
    role: 'EDITOR',
    grants: 'Upload documents, search and preview the corpus, chat with full corpus scope.',
  },
  {
    role: 'VIEWER',
    grants: 'Search and preview the corpus. No download permission; chat results are read-only.',
  },
]

const STATES: Array<{ code: string; meaning: string }> = [
  {
    code: 'QUEUED',
    meaning: 'Accepted and waiting for a worker slot. Nothing has been read yet.',
  },
  {
    code: 'FETCHING',
    meaning: 'The original file is being pulled from storage to the parser.',
  },
  {
    code: 'EXTRACTING',
    meaning: 'Pages and rows are being parsed and indexed. The pipeline popover shows live progress.',
  },
  {
    code: 'READY',
    meaning: 'Indexed and searchable. The revision letter marks which extraction the index holds.',
  },
  {
    code: 'FAILED',
    meaning: 'Processing stopped — the cause is on the run. Upload the file again to retry.',
  },
]

const CODES: Array<{ code: string; meaning: string }> = [
  {
    code: 'DOC-0142',
    meaning: "A document's permanent identifier inside this workspace. Cited in answers.",
  },
  {
    code: 'WS-GC-01',
    meaning: 'The workspace you are in. Every search and answer is scoped to it.',
  },
  {
    code: 'RUN-0231',
    meaning: 'One processing pass over one document — upload, re-index, or delete.',
  },
  {
    code: 'REV C',
    meaning: 'Which extraction of a file the index holds; a re-index issues the next letter.',
  },
]

const KEYS: Array<[string, string]> = [
  ['⌘K', 'Focus document search from anywhere.'],
  ['ESC', 'Close the viewer, dialogs, and popovers.'],
]

function Section({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string
  title: string
  children: React.ReactNode
}) {
  return (
    <section className="panel overflow-hidden">
      <div className="border-b border-line px-5 py-3.5">
        <p className="eyebrow">{eyebrow}</p>
        <h2 className="mt-0.5 font-condensed text-sm font-bold uppercase tracking-[0.06em] text-ink">
          {title}
        </h2>
      </div>
      {children}
    </section>
  )
}

export function HelpView() {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Section eyebrow="Set by the workspace owner" title="Roles">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Role</th>
              <th scope="col">Can do</th>
            </tr>
          </thead>
          <tbody>
            {ROLES.map(({ role, grants }) => (
              <tr key={role}>
                <td>
                  <span className="stamp">{role}</span>
                </td>
                <td className="leading-6">{grants}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section eyebrow="The life of one upload" title="Document states">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">State</th>
              <th scope="col">Meaning</th>
            </tr>
          </thead>
          <tbody>
            {STATES.map(({ code, meaning }) => (
              <tr key={code}>
                <td>
                  <span className="stamp">{code}</span>
                </td>
                <td className="leading-6">{meaning}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section eyebrow="Every machine identifier you will see" title="The codes">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Code</th>
              <th scope="col">Meaning</th>
            </tr>
          </thead>
          <tbody>
            {CODES.map(({ code, meaning }) => (
              <tr key={code}>
                <td>
                  <span className="stamp whitespace-nowrap">{code}</span>
                </td>
                <td className="leading-6">{meaning}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section eyebrow="Two shortcuts carry the whole app" title="Keyboard">
        <table className="table">
          <tbody>
            {KEYS.map(([key, meaning]) => (
              <tr key={key}>
                <td className="w-20">
                  <span className="stamp whitespace-nowrap">{key}</span>
                </td>
                <td className="leading-6">{meaning}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </div>
  )
}
