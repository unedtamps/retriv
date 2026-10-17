import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import App from './App'
import { readSessionToken, writeSessionToken } from './session'
import type { CurrentUser, Workspace } from './types'

const admin: CurrentUser = {
  id: 'user-admin',
  username: 'admin',
  role: 'admin',
  is_active: true,
}

const owner: CurrentUser = {
  id: 'user-owner',
  username: 'maya',
  role: 'user',
  is_active: true,
}

const workspace: Workspace = {
  id: 'workspace-1',
  slug: 'launch-notes',
  name: 'Launch notes',
  description: 'Product planning and release research.',
  created_at: '2026-09-23T08:00:00Z',
  membership_role: 'owner' as const,
  can_upload: true,
  can_search_context: true,
  can_manage_members: true,
  can_delete: true,
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function installApi(
  user: CurrentUser = admin,
  workspaces: Workspace[] = [workspace],
  uploadResponse?: Response,
  processingResponses: { active?: unknown; recent?: unknown } = {},
  documents: { list?: unknown; deleted?: string[] } = {},
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/v1/auth/token') {
      expect(init?.body).toBeInstanceOf(URLSearchParams)
      return json({ access_token: 'signed-token', token_type: 'bearer' })
    }
    if (url === '/v1/auth/me') return json(user)
    if (url.startsWith('/v1/workspaces?')) {
      return json({ items: workspaces, total: workspaces.length, offset: 0, limit: 100 })
    }
    // GET only: the upload POST shares this exact path, and answering it
    // from here silently broke the duplicate-upload and session tests.
    const documentList = url.match(/^\/v1\/workspaces\/[^/]+\/documents(\?|$)/)
    if (documentList !== null && (init?.method === undefined || init.method === 'GET')) {
      return json(documents.list ?? { items: [], total: 0, offset: 0, limit: 50 })
    }
    const documentDelete = url.match(/^\/v1\/workspaces\/[^/]+\/documents\/([^/?]+)$/)
    if (documentDelete !== null && init?.method === 'DELETE') {
      documents.deleted?.push(documentDelete[1])
      // The API stops reporting a document once its delete is accepted, so
      // the mock has to as well. A stateless mock let the row reappear on
      // the refetch that onChanged triggers, and the test only passed by
      // timing.
      if (documents.list !== undefined) {
        const list = documents.list as { items: Array<{ id: string }> }
        list.items = list.items.filter((item) => item.id !== documentDelete[1])
      }
      return new Response(null, { status: 204 })
    }
    if (url === '/v1/processing-runs/active') {
      return json(processingResponses.active ?? { item: null })
    }
    if (url === '/v1/processing-runs/recent') {
      return json(processingResponses.recent ?? {
        items: [],
        total: 0,
        since: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
      })
    }
    if (url === '/v1/workspaces/workspace-1/documents' && init?.method === 'POST') {
      return uploadResponse ?? json({
        id: 'document-1',
        workspace_id: workspace.id,
        original_filename: 'context.pdf',
        detected_media_type: 'application/pdf',
        size_bytes: 18,
        sha256: 'a'.repeat(64),
        status: 'ready',
        created_by_user_id: user.id,
        created_at: '2026-09-24T08:00:00Z',
        duplicate: false,
      }, 201)
    }
    if (url === '/v1/workspaces/workspace-1/members') return json({ items: [] })
    if (url.startsWith('/v1/admin/users')) return json({ items: [], total: 0, offset: 0, limit: 50 })
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('dashboard application', () => {
  it('lands on the governance dashboard after login and sends the session token', async () => {
    window.history.replaceState(null, '', '/')
    const fetchMock = installApi()
    const user = userEvent.setup()

    render(<App />)

    await user.type(screen.getByLabelText('Username'), 'admin')
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple')
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    expect(await screen.findByRole('heading', { name: 'Access, ownership, and context boundaries.' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/dashboard')
    expect(screen.getByRole('button', { name: 'New workspace' })).toBeInTheDocument()
    expect(screen.getByText('User directory')).toBeInTheDocument()
    expect(readSessionToken()).toBe('signed-token')
    const meCall = fetchMock.mock.calls.find(([input]) => String(input) === '/v1/auth/me')
    if (meCall === undefined) throw new Error('Expected a current-user request')
    const headers = meCall[1]?.headers
    if (!(headers instanceof Headers)) throw new Error('Expected request headers')
    expect(headers.get('Authorization')).toBe('Bearer signed-token')
  })

  it('shows workspace capabilities without admin controls to a regular user', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    installApi(owner, [
      workspace,
      {
        ...workspace,
        id: 'workspace-2',
        slug: 'shared-reference',
        name: 'Shared reference',
        membership_role: 'viewer',
        can_upload: false,
        can_manage_members: false,
        can_delete: false,
      },
      {
        ...workspace,
        id: 'workspace-3',
        slug: 'company-catalog',
        name: 'Company catalog',
        membership_role: null,
        can_upload: false,
        can_search_context: false,
        can_manage_members: false,
        can_delete: false,
      },
    ])

    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Access, ownership, and context boundaries.' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New workspace' })).not.toBeInTheDocument()
    expect(screen.queryByText('User directory')).not.toBeInTheDocument()
    expect(screen.getAllByText('Shared reference')).toHaveLength(2)
    expect(screen.getAllByText('Company catalog')).toHaveLength(2)
    const ownerRow = screen.getByRole('row', { name: /Launch notes/ })
    await user.click(within(ownerRow).getByRole('button', { name: 'Open' }))
    expect(await screen.findByRole('heading', { name: 'Workspace members' })).toBeInTheDocument()
  })

  it('clears an expired session and returns to sign in', async () => {
    writeSessionToken('expired-token')
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/v1/auth/me') return json({ detail: 'Invalid or expired token' }, 401)
      if (url.startsWith('/v1/workspaces?')) {
        return json({ items: [], total: 0, offset: 0, limit: 100 })
      }
      throw new Error(`Unexpected request: ${url}`)
    }))

    render(<App />)

    expect(await screen.findByRole('button', { name: 'Continue' })).toBeInTheDocument()
    await waitFor(() => expect(readSessionToken()).toBeNull())
  })

  it('keeps a direct upload route through login and sends multipart only to an uploadable workspace', async () => {
    window.history.replaceState(null, '', '/upload-context')
    const fetchMock = installApi(owner, [
      workspace,
      { ...workspace, id: 'workspace-2', name: 'Read only', membership_role: 'viewer', can_upload: false },
      { ...workspace, id: 'workspace-3', name: 'Catalog only', membership_role: null, can_upload: false },
    ])
    const user = userEvent.setup()

    render(<App />)
    expect(window.location.pathname).toBe('/upload-context')
    await user.type(screen.getByLabelText('Username'), 'maya')
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple')
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    expect(await screen.findByRole('heading', { name: 'Upload context', level: 1 })).toBeInTheDocument()
    const select = screen.getByRole('combobox', { name: 'Workspace' })
    expect(within(select).getByRole('option', { name: /Launch notes/ })).toBeInTheDocument()
    expect(within(select).queryByRole('option', { name: /Read only|Catalog only/ })).not.toBeInTheDocument()
    await user.selectOptions(select, workspace.id)
    const file = new File(['a representative PDF'], 'context.pdf', { type: 'application/pdf' })
    await user.upload(screen.getByLabelText('Document'), file)
    expect(screen.getByRole('button', { name: 'Upload document' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: 'Upload document' }))

    expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/v1/workspaces/workspace-1/documents')).toHaveLength(1)

    expect(await screen.findByRole('heading', { name: 'Document accepted' })).toBeInTheDocument()
    expect(screen.getByText(/Extraction and indexing happen asynchronously/)).toBeInTheDocument()
    expect(window.location.pathname).toBe('/upload-context')
    const uploadCall = fetchMock.mock.calls.find(([input]) => String(input) === '/v1/workspaces/workspace-1/documents')
    if (uploadCall === undefined) throw new Error('Expected an upload request')
    const headers = uploadCall[1]?.headers
    if (!(headers instanceof Headers)) throw new Error('Expected request headers')
    expect(headers.get('Authorization')).toBe('Bearer signed-token')
    expect(headers.get('Content-Type')).toBeNull()
    const body = uploadCall[1]?.body
    if (!(body instanceof FormData)) throw new Error('Expected multipart form data')
    expect((body.get('file') as File).name).toBe('context.pdf')
  })

  it('accepts a CSV for upload, which is stored as rows rather than indexed', async () => {
    // CSV replaced XLSX. A spreadsheet was accepted by the form and then
    // rejected by the extractor, so the picker must not offer a type the
    // pipeline cannot read, and must offer the one it now can.
    const user = userEvent.setup()
    const fetchMock = installApi(owner, [workspace])

    // Navigate explicitly rather than inheriting the path a previous test
    // happened to leave behind.
    window.history.pushState({}, '', '/upload-context')
    render(<App />)
    await user.type(screen.getByLabelText('Username'), 'maya')
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByRole('heading', { name: 'Upload context', level: 1 })).toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Workspace' }), workspace.id)

    const file = new File(['id,amount\n1,10\n'], 'sales.csv', { type: 'text/csv' })
    await user.upload(screen.getByLabelText('Document'), file)
    expect(screen.getByRole('button', { name: 'Upload document' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: 'Upload document' }))

    expect(await screen.findByRole('heading', { name: 'Document accepted' })).toBeInTheDocument()
    const uploadCall = fetchMock.mock.calls.find(([input]) => String(input) === '/v1/workspaces/workspace-1/documents')
    if (uploadCall === undefined) throw new Error('Expected an upload request')
    const body = uploadCall[1]?.body
    if (!(body instanceof FormData)) throw new Error('Expected multipart form data')
    expect((body.get('file') as File).name).toBe('sales.csv')
  })

  it('rejects a spreadsheet extension the pipeline cannot read', async () => {
    const user = userEvent.setup()
    // The stub is needed for its side effect; nothing here inspects the calls.
    installApi(owner, [workspace])

    // Navigate explicitly rather than inheriting the path a previous test
    // happened to leave behind.
    window.history.pushState({}, '', '/upload-context')
    render(<App />)
    await user.type(screen.getByLabelText('Username'), 'maya')
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByRole('heading', { name: 'Upload context', level: 1 })).toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Workspace' }), workspace.id)

    // userEvent.upload honours the input's accept attribute and silently
    // drops a file it excludes, so the change is fired directly to reach the
    // application's own check.
    fireEvent.change(screen.getByLabelText('Document'), {
      target: { files: [new File(['x'], 'budget.xlsx', { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })] },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent('Only PDF, DOCX and CSV')
    expect(screen.getByRole('button', { name: 'Upload document' })).toBeDisabled()
  })

  it('shows how many rows a CSV holds, and says plainly how a CSV is used', async () => {
    // A CSV's content is rows, not chunks, so the run reports row_count and
    // the list shows it. Without this a CSV reads as a bare "Ready" with
    // nothing behind it, which looks like an upload that did nothing.
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    const base = {
      workspace_id: workspace.id,
      size_bytes: 10,
      sha256: 'a'.repeat(64),
      status: 'ready',
      created_by_user_id: admin.id,
      created_at: '2026-01-01T00:00:00Z',
      duplicate: false,
    }
    installApi(owner, [workspace], undefined, {}, {
      list: {
        items: [
          { ...base, id: 'doc-1', original_filename: 'sales.csv', detected_media_type: 'text/csv', row_count: 900 },
          { ...base, id: 'doc-2', original_filename: 'UNDP.pdf', detected_media_type: 'application/pdf', row_count: 0 },
        ],
        total: 2,
        offset: 0,
        limit: 50,
      },
    })

    const user = userEvent.setup()
    render(<App />)

    const row = await screen.findByRole('row', { name: /Launch notes/ })
    await user.click(within(row).getByRole('button', { name: 'Open' }))

    expect(await screen.findByText('sales.csv')).toBeInTheDocument()
    expect(screen.getByText(/900 rows/)).toBeInTheDocument()
    // A document that is not a CSV has no rows to report, and must not be
    // given a "0 rows" label that reads as a broken counter.
    expect(screen.getByText('UNDP.pdf')).toBeInTheDocument()
    // Exactly one row label, because a document that is not a CSV has no
    // rows to report. Matching on a bare "0 rows" would also match "900
    // rows", so the count is what actually pins this.
    expect(screen.getAllByText(/rows/)).toHaveLength(1)
  })

  it('no longer tells the user spreadsheets are unsupported', async () => {
    // The old copy said spreadsheets were not yet indexed, which was both
    // stale and now wrong in a second way: a CSV is not indexed on purpose.
    installApi(owner, [workspace])
    const user = userEvent.setup()
    window.history.pushState({}, '', '/upload-context')
    render(<App />)
    await user.type(screen.getByLabelText('Username'), 'maya')
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple')
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    expect(
      await screen.findByRole('heading', { name: 'Upload context', level: 1 })
    ).toBeInTheDocument()
    const help = screen.getByText(/PDF, DOCX or CSV/)
    expect(help).toBeInTheDocument()
    expect(help).toHaveTextContent(/queried with SQL/)
    expect(screen.queryByText(/not yet indexed/)).not.toBeInTheDocument()
  })

  it('navigates to the upload route from the dashboard and follows browser history', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    installApi(owner)
    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Access, ownership, and context boundaries.' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(screen.getByRole('link', { name: 'Upload context' }))
    expect(window.location.pathname).toBe('/upload-context')
    expect(screen.getByRole('heading', { name: 'Upload context', level: 1 })).toBeInTheDocument()
    window.history.replaceState(null, '', '/dashboard')
    window.dispatchEvent(new PopStateEvent('popstate'))
    expect(await screen.findByRole('heading', { name: 'Access, ownership, and context boundaries.' })).toBeInTheDocument()
  })

  it('shows no upload form when the signed-in user has no eligible workspace', async () => {
    window.history.replaceState(null, '', '/upload-context')
    writeSessionToken('admin-token')
    installApi(admin, [{ ...workspace, membership_role: null, can_upload: false }])
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'No workspaces available for upload' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Upload document' })).not.toBeInTheDocument()
  })

  it('shows duplicate-upload and validation responses without claiming indexing is finished', async () => {
    window.history.replaceState(null, '', '/upload-context')
    writeSessionToken('member-token')
    const duplicate = json({
      id: 'existing-document',
      workspace_id: workspace.id,
      original_filename: 'stored.pdf',
      detected_media_type: 'application/pdf',
      size_bytes: 8,
      sha256: 'b'.repeat(64),
      status: 'ready',
      created_by_user_id: owner.id,
      created_at: '2026-09-24T08:00:00Z',
      duplicate: true,
    })
    installApi(owner, [workspace], duplicate)
    const user = userEvent.setup()
    render(<App />)
    await screen.findByRole('heading', { name: 'Upload context', level: 1 })
    await user.selectOptions(screen.getByRole('combobox', { name: 'Workspace' }), workspace.id)
    await user.upload(screen.getByLabelText('Document'), new File(['pdf'], 'stored.pdf', { type: 'application/pdf' }))
    await user.click(screen.getByRole('button', { name: 'Upload document' }))
    expect(await screen.findByRole('heading', { name: 'Document already exists' })).toBeInTheDocument()

    installApi(owner, [workspace], json({ detail: 'The file is invalid.', code: 'invalid_document' }, 422))
    await user.upload(screen.getByLabelText('Document'), new File(['invalid'], 'bad.pdf', { type: 'application/pdf' }))
    await user.click(screen.getByRole('button', { name: 'Upload document' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The file is invalid.')
  })

  it('clears a session rejected by the upload endpoint', async () => {
    window.history.replaceState(null, '', '/upload-context')
    writeSessionToken('expired-token')
    installApi(owner, [workspace], json({ detail: 'Invalid or expired token' }, 401))
    const user = userEvent.setup()
    render(<App />)

    await screen.findByRole('heading', { name: 'Upload context', level: 1 })
    await user.selectOptions(screen.getByRole('combobox', { name: 'Workspace' }), workspace.id)
    await user.upload(screen.getByLabelText('Document'), new File(['pdf'], 'context.pdf', { type: 'application/pdf' }))
    await user.click(screen.getByRole('button', { name: 'Upload document' }))
    expect(await screen.findByRole('button', { name: 'Continue' })).toBeInTheDocument()
    expect(readSessionToken()).toBeNull()
    expect(window.location.pathname).toBe('/upload-context')
  })

  it('shows the active process and recent workspace activity in notifications', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    const activeRun = {
      id: 'run-active',
      workspace_id: workspace.id,
      document_id: 'document-active',
      filename: 'report-processing.pdf',
      job_kind: 'extract',
      status: 'embedding',
      attempts: 1,
      chunk_count: 4,
      embedded_chunk_count: 2,
      created_at: '2026-09-24T08:00:00Z',
      started_at: '2026-09-24T08:00:01Z',
      completed_at: null,
    }
    const completedRun = {
      ...activeRun,
      id: 'run-complete',
      document_id: 'document-complete',
      filename: 'finished-report.pdf',
      status: 'ready',
      embedded_chunk_count: 4,
      completed_at: '2026-09-24T08:01:00Z',
    }
    installApi(owner, [workspace], undefined, {
      active: { item: activeRun },
      recent: {
        items: [activeRun, completedRun],
        total: 2,
        since: '2026-09-24T07:00:00Z',
      },
    })
    const user = userEvent.setup()

    render(<App />)

    await screen.findByRole('heading', { name: 'Access, ownership, and context boundaries.' })
    const notificationButton = await screen.findByRole('button', {
      name: 'Open processing notifications, 2 recent runs',
    })
    await user.click(notificationButton)

    expect(screen.getByRole('dialog', { name: 'Processing notifications' })).toBeInTheDocument()
    expect(screen.getByText('Currently processing')).toBeInTheDocument()
    expect(screen.getByText('Recent activity')).toBeInTheDocument()
    expect(screen.getByText('report-processing.pdf')).toBeInTheDocument()
    expect(screen.getByText('finished-report.pdf')).toBeInTheDocument()
    expect(screen.getByText('2/4 embeddings')).toBeInTheDocument()
    expect(screen.getAllByText('report-processing.pdf')).toHaveLength(1)
  })

  it('stops requesting processing runs once loaded instead of polling', async () => {
    // Fake timers are what make this meaningful. The old timer was ten
    // seconds, so without advancing the clock a short test would pass
    // against the polling code too and prove nothing.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      window.history.replaceState(null, '', '/dashboard')
      writeSessionToken('member-token')
      const fetchMock = installApi(owner, [workspace])
      const countProcessingRequests = () => fetchMock.mock.calls.filter(([input]) => {
        const url = String(input)
        return url === '/v1/processing-runs/active' || url === '/v1/processing-runs/recent'
      }).length

      render(<App />)

      // The bell renders before its data arrives, so wait on the request
      // itself rather than on the button being present.
      await waitFor(() => expect(countProcessingRequests()).toBeGreaterThan(0))
      const afterMount = countProcessingRequests()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000)
      })

      expect(countProcessingRequests()).toBe(afterMount)
    } finally {
      vi.useRealTimers()
    }
  })

  it('reloads processing activity only when Refresh is pressed', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    const firstRun = {
      id: 'run-first',
      workspace_id: workspace.id,
      document_id: 'document-first',
      filename: 'first-upload.pdf',
      job_kind: 'extract',
      status: 'ready',
      attempts: 1,
      chunk_count: 2,
      embedded_chunk_count: 2,
      created_at: '2026-09-24T08:00:00Z',
      started_at: '2026-09-24T08:00:01Z',
      completed_at: '2026-09-24T08:00:30Z',
    }
    // Kept mutable on purpose: installApi reads these fields per request,
    // so swapping them between clicks is what the refresh has to pick up.
    const processingResponses = {
      active: { item: null },
      recent: {
        items: [firstRun],
        total: 1,
        since: '2026-09-24T07:00:00Z',
      },
    }
    installApi(owner, [workspace], undefined, processingResponses)
    const user = userEvent.setup()

    render(<App />)

    await user.click(await screen.findByRole('button', {
      name: 'Open processing notifications, 1 recent runs',
    }))
    expect(await screen.findByText('first-upload.pdf')).toBeInTheDocument()

    processingResponses.recent = {
      items: [{
        ...firstRun,
        id: 'run-second',
        document_id: 'document-second',
        filename: 'second-upload.pdf',
      }],
      total: 1,
      since: '2026-09-24T07:00:00Z',
    }

    await user.click(screen.getByRole('button', { name: 'Refresh processing activity' }))

    expect(await screen.findByText('second-upload.pdf')).toBeInTheDocument()
    expect(screen.queryByText('first-upload.pdf')).not.toBeInTheDocument()
  })

  it('closes the slide-in navigation with Escape and the backdrop', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    installApi(owner)
    const user = userEvent.setup()

    render(<App />)

    await screen.findByRole('heading', { name: 'Access, ownership, and context boundaries.' })
    const openNavigation = screen.getByRole('button', { name: 'Open navigation' })
    await user.click(openNavigation)
    expect(screen.getByRole('dialog', { name: 'Primary navigation' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: 'Primary navigation' })).not.toBeInTheDocument()
    expect(openNavigation).toHaveAttribute('aria-expanded', 'false')

    await user.click(openNavigation)
    await user.click(screen.getByRole('button', { name: 'Close navigation' }))
    expect(screen.queryByRole('dialog', { name: 'Primary navigation' })).not.toBeInTheDocument()
  })

  it('lists workspace documents and deletes one after confirmation', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    const deleted: string[] = []
    const document = {
      id: 'document-1',
      workspace_id: workspace.id,
      original_filename: 'launch-review.pdf',
      detected_media_type: 'application/pdf',
      size_bytes: 2048,
      sha256: 'a'.repeat(64),
      status: 'ready',
      created_by_user_id: 'user-owner',
      created_at: '2026-09-24T08:00:00Z',
      duplicate: false,
    }
    installApi(owner, [workspace], undefined, {}, {
      list: { items: [document], total: 1, offset: 0, limit: 50 },
      deleted,
    })
    const user = userEvent.setup()

    render(<App />)

    const row = await screen.findByRole('row', { name: /Launch notes/ })
    await user.click(within(row).getByRole('button', { name: 'Open' }))

    expect(await screen.findByText('launch-review.pdf')).toBeInTheDocument()
    expect(screen.getByText(/Ready/)).toBeInTheDocument()

    // Deletion is permanent, so it goes through the confirmation dialog.
    await user.click(screen.getByRole('button', { name: 'Delete launch-review.pdf' }))
    expect(screen.getByRole('dialog', { name: /Delete this document/ })).toBeInTheDocument()

    // Cancelling must not call the API.
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(deleted).toEqual([])
    expect(screen.getByText('launch-review.pdf')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Delete launch-review.pdf' }))
    await user.click(screen.getByRole('button', { name: 'Delete document' }))

    expect(deleted).toEqual(['document-1'])
    await waitFor(() =>
      expect(screen.queryByText('launch-review.pdf')).not.toBeInTheDocument(),
    )
  })

  it('withholds the delete control from a workspace the role cannot delete in', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    const viewerWorkspace: Workspace = {
      ...workspace,
      id: 'workspace-viewer',
      slug: 'shared-reference',
      name: 'Shared reference',
      membership_role: 'viewer',
      can_upload: false,
      can_search_context: true,
      can_manage_members: false,
      can_delete: false,
    }
    // Deliberately a platform admin. An admin bypass is the exact mistake
    // this guards: load_workspace_access has none, so the button would 404.
    installApi(admin, [viewerWorkspace], undefined, {}, {
      list: {
        items: [{
          id: 'document-viewer',
          workspace_id: viewerWorkspace.id,
          original_filename: 'shared-notes.pdf',
          detected_media_type: 'application/pdf',
          size_bytes: 1024,
          sha256: 'b'.repeat(64),
          status: 'ready',
          created_by_user_id: 'user-owner',
          created_at: '2026-09-24T08:00:00Z',
          duplicate: false,
        }],
        total: 1,
        offset: 0,
        limit: 50,
      },
    })
    const user = userEvent.setup()

    render(<App />)

    const row = await screen.findByRole('row', { name: /Shared reference/ })
    await user.click(within(row).getByRole('button', { name: 'Open' }))

    // A viewer may read the list; only the destructive control is withheld.
    expect(await screen.findByText('shared-notes.pdf')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete shared-notes.pdf' })).toBeNull()
  })
})
