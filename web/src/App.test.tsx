import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import App from './App'
import { readSession, readSessionToken, writeSession, writeSessionToken } from './session'
import { buildDocx, DOCX_MEDIA_TYPE } from './test/docxFixture'
import { loadChatHistory } from './lib/chatHistoryStore'
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
  member_count: 3,
  can_upload: true,
  can_download: true,
  can_search_context: true,
  can_manage_members: true,
  can_delete: true,
}

async function gotoOverview(
  user: ReturnType<typeof userEvent.setup>,
  heading = 'Launch notes',
) {
  await screen.findByRole('button', { name: 'Open navigation' })
  await user.click(screen.getByRole('button', { name: 'Open navigation' }))
  await user.click(
    within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('button', {
      name: 'Overview',
    }),
  )
  const close = screen.queryByRole('button', { name: 'Close navigation drawer' })
  if (close !== null) await user.click(close)
  return screen.findByRole('heading', { name: heading, level: 1 })
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
  processingResponses: { runs?: unknown } = {},
  documents: {
    list?: unknown
    deleted?: string[]
    search?: unknown
    download?: 'ok' | 'forbidden'
    /** Serve these bytes instead of the PDF fixture, for the Word preview path. */
    downloadBlob?: Blob
  } = {},
  members: { list?: unknown } = {},
  auth: { refresh?: 'ok' | 'rejected'; meFirst?: 'ok' | 'expired' } = {},
  chat: {
    holdBeforeFinalMs?: number
    toolCalls?: { name: string; arguments: Record<string, unknown>; preview: string }[]
  } = {},
) {
  const refreshes: string[] = []
  const logouts: string[] = []
  let meCalls = 0
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/v1/auth/token') {
      expect(init?.body).toBeInstanceOf(URLSearchParams)
      return json({
        access_token: 'signed-token',
        token_type: 'bearer',
        expires_in: 3600,
        refresh_token: `rt_${'a'.repeat(43)}`,
        refresh_expires_in: 2592000,
      })
    }
    if (url === '/v1/auth/refresh') {
      if (auth.refresh === 'rejected') {
        return json({ code: 'invalid_credentials' }, 401)
      }
      refreshes.push(String(init?.body))
      return json({
        access_token: 'refreshed-token',
        token_type: 'bearer',
        expires_in: 3600,
        refresh_token: `rt_${'b'.repeat(43)}`,
        refresh_expires_in: 2592000,
      })
    }
    if (url === '/v1/auth/logout') {
      logouts.push(String(init?.body))
      return new Response(null, { status: 204 })
    }
    if (url === '/v1/auth/me') {
      meCalls += 1
      if (auth.meFirst === 'expired' && meCalls === 1) {
        return json({ code: 'invalid_credentials' }, 401)
      }
      return json(user)
    }
    if (url.startsWith('/v1/workspaces?')) {
      return json({ items: workspaces, total: workspaces.length, offset: 0, limit: 100 })
    }
    if (url.endsWith('/agent/chat')) {
      const encoder = new TextEncoder()
      const stream = new ReadableStream<Uint8Array>({
        async start(controller) {
          init?.signal?.addEventListener('abort', () => {
            try {
              controller.error(new DOMException('The request was aborted.', 'AbortError'))
            } catch {
              // the stream was already closed
            }
          })
          const toolCalls = chat.toolCalls ?? [
            { name: 'search_context', arguments: { query: 'anything' }, preview: 'top hit' },
          ]
          const early = [
            { type: 'turn_start', turns_allowed: 12 },
            ...toolCalls.flatMap((call) => [
              { type: 'tool_call', name: call.name, arguments: call.arguments },
              {
                type: 'tool_result',
                name: call.name,
                length: call.preview.length,
                truncated: false,
                preview: call.preview,
              },
            ]),
          ]
          for (const frame of early) {
            controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`))
          }
          if (chat.holdBeforeFinalMs !== undefined) {
            await new Promise((resolve) => setTimeout(resolve, chat.holdBeforeFinalMs))
          }
          const final = {
            type: 'final',
            content: 'The workspace holds one report.\n\n## Findings\n\n- Revenue rose by a tenth.',
          }
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(final)}\n\n`))
          controller.close()
        },
      })
      return new Response(stream, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      })
    }
    if (url === '/v1/api-keys') {
      return json({ items: [], total: 0, offset: 0, limit: 50 })
    }
    if (url.startsWith('/v1/admin/workspaces?') || url === '/v1/admin/workspaces') {
      if (init?.method === 'POST') throw new Error(`Unexpected request: ${url}`)
      return json({
        items: workspaces.map((workspace) => ({
          ...workspace,
          members: [
            {
              user_id: 'member-1',
              username: 'workspace-owner',
              role: 'owner' as const,
              granted_by_user_id: user.id,
              created_at: '2026-09-23T08:00:00Z',
              updated_at: '2026-09-23T08:00:00Z',
            },
          ],
        })),
        total: workspaces.length,
        offset: 0,
        limit: 100,
      })
    }
    const adminMemberPut = url.match(/^\/v1\/admin\/workspaces\/[^/]+\/members\/([^/]+)$/)
    if (adminMemberPut !== null && init?.method === 'PUT') {
      return json({
        user_id: adminMemberPut[1],
        username: 'workspace-owner',
        role: 'viewer',
        granted_by_user_id: user.id,
        created_at: '2026-09-23T08:00:00Z',
        updated_at: '2026-09-23T08:00:00Z',
      })
    }
    const documentList = url.match(/^\/v1\/workspaces\/[^/]+\/documents(\?|$)/)
    if (documentList !== null && (init?.method === undefined || init.method === 'GET')) {
      return json(documents.list ?? { items: [], total: 0, offset: 0, limit: 50 })
    }
    const downloadMatch = url.match(/^\/v1\/workspaces\/[^/]+\/documents\/([^/?]+)\/download/)
    if (downloadMatch !== null) {
      if (documents.download === 'forbidden') return json({ code: 'insufficient_workspace_permission' }, 403)
      const body = documents.downloadBlob
        ?? new Blob(['%PDF-1.7 fake'], { type: 'application/pdf' })
      // jsdom's Response stringifies a Blob body instead of reading it, so the
      // bytes have to go in as an ArrayBuffer.
      return new Response(await body.arrayBuffer(), {
        status: 200,
        headers: { 'Content-Type': body.type || 'application/pdf' },
      })
    }
    const documentDelete = url.match(/^\/v1\/workspaces\/[^/]+\/documents\/([^/?]+)$/)
    if (documentDelete !== null && init?.method === 'DELETE') {
      documents.deleted?.push(documentDelete[1])
      if (documents.list !== undefined) {
        const list = documents.list as { items: Array<{ id: string }> }
        list.items = list.items.filter((item) => item.id !== documentDelete[1])
      }
      return new Response(null, { status: 204 })
    }
    const reindexMatch = url.match(/^\/v1\/workspaces\/[^/]+\/documents\/([^/?]+)\/reindex$/)
    if (reindexMatch !== null && init?.method === 'POST') {
      const id = reindexMatch[1]
      const list = documents.list as { items: Array<Record<string, unknown>> } | undefined
      const existing = list?.items.find((item) => item.id === id)
      return json({ ...existing, status: 'processing' }, 202)
    }
    if (url.startsWith('/v1/documents/search')) {
      return json(documents.search ?? { items: [], total: 0 })
    }
    if (url === '/v1/processing-runs') {
      return json(processingResponses.runs ?? { items: [], total: 0 })
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
    const ownerMemberPut = url.match(/^\/v1\/workspaces\/[^/]+\/members\/([^/]+)$/)
    if (ownerMemberPut !== null && init?.method === 'PUT') {
      return json({
        user_id: ownerMemberPut[1],
        username: 'nada',
        role: 'editor',
        granted_by_user_id: user.id,
        created_at: '2026-09-23T08:00:00Z',
        updated_at: '2026-09-23T09:00:00Z',
      })
    }
    if (url === '/v1/workspaces/workspace-1/members') {
      return json(members.list ?? { items: [] })
    }
    if (url.startsWith('/v1/admin/users')) {
      return json({
        items: [
          {
            id: 'user-second',
            username: 'workspace-owner',
            role: 'user',
            is_active: true,
            created_at: '2026-09-23T08:00:00Z',
          },
          {
            id: 'user-admin-entry',
            username: 'root',
            role: 'admin',
            is_active: true,
            created_at: '2026-09-23T08:00:00Z',
          },
        ],
        total: 2,
        offset: 0,
        limit: 50,
      })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return Object.assign(fetchMock, { refreshes, logouts })
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

    expect(await screen.findByRole('heading', { name: 'Chat', level: 1 })).toBeInTheDocument()
    expect(window.location.search).toContain('view=chat')
    expect(window.location.pathname).toBe('/dashboard')
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    expect(screen.getByRole('button', { name: 'New workspace' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Users' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Close navigation drawer' }))
    expect(readSessionToken()).toBe('signed-token')
    const meCall = fetchMock.mock.calls.find(([input]) => String(input) === '/v1/auth/me')
    if (meCall === undefined) throw new Error('Expected a current-user request')
    const headers = meCall[1]?.headers
    if (!(headers instanceof Headers)) throw new Error('Expected request headers')
    expect(headers.get('Authorization')).toBe('Bearer signed-token')
  })

  it('opens a workspace-contextual upload drawer from the overview', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    const fetchMock = installApi(owner)
    const user = userEvent.setup()

    render(<App />)

    await gotoOverview(user)
    await user.click(screen.getByRole('button', { name: 'Upload document' }))

    const drawer = await screen.findByRole('dialog', { name: 'Upload document' })
    expect(within(drawer).queryByRole('combobox', { name: 'Workspace' })).not.toBeInTheDocument()

    const file = new File(['a representative PDF'], 'drawer-context.pdf', { type: 'application/pdf' })
    await user.upload(within(drawer).getByLabelText('Document'), file)
    await user.click(within(drawer).getByRole('button', { name: 'Upload document' }))

    expect(await within(drawer).findByRole('heading', { name: 'Document accepted' })).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/v1/workspaces/workspace-1/documents')).toHaveLength(1)
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
        can_download: false,
        can_search_context: false,
        can_manage_members: false,
        can_delete: false,
      },
    ])

    const user = userEvent.setup()
    render(<App />)

    expect(await gotoOverview(user)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    expect(screen.queryByRole('button', { name: 'New workspace' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Users' })).not.toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Current workspace Launch notes, switch workspace' }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Close navigation drawer' }))
    await user.click(screen.getByRole('button', { name: 'Manage access' }))
    expect(await screen.findByRole('heading', { name: 'Members', level: 1 })).toBeInTheDocument()
    expect(screen.getByText('Register of members')).toBeInTheDocument()
  })

  it('gives an admin without memberships a platform overview instead of workspace navigation', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('admin-token')
    const fetchMock = installApi(admin, [
      {
        ...workspace,
        membership_role: null,
        can_upload: false,
        can_download: false,
        can_search_context: false,
        can_manage_members: false,
        can_delete: false,
      },
    ])
    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Workspaces', level: 1 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    expect(screen.queryByRole('button', { name: 'Overview' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Documents' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Upload context' })).not.toBeInTheDocument()
    expect(screen.queryByText('Current workspace')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'API keys' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New workspace' })).toBeInTheDocument()

    const platformSection = screen.getByRole('region', { name: 'All workspaces' })
    expect(within(platformSection).getAllByText('Launch notes').length).toBeGreaterThanOrEqual(1)

    await user.click(
      within(platformSection).getByRole('button', { name: 'View members of Launch notes' }),
    )
    const register = screen.getByRole('dialog', { name: 'Workspace members' })
    expect(within(register).getByText('workspace-owner')).toBeInTheDocument()
    expect(within(register).getByText('owner')).toBeInTheDocument()
    await user.click(within(register).getByRole('button', { name: 'Close Workspace members' }))
    expect(screen.queryByRole('dialog', { name: 'Workspace members' })).not.toBeInTheDocument()

    await user.click(
      within(platformSection).getByRole('button', { name: 'Add a member to Launch notes' }),
    )
    const grant = screen.getByRole('dialog', { name: 'Add a member' })
    expect(await within(grant).findByRole('button', { name: /workspace-owner/ })).toBeInTheDocument()
    expect(within(grant).queryByRole('button', { name: /root/ })).not.toBeInTheDocument()
    expect(within(grant).getByText('1 of 2 users')).toBeInTheDocument()
    await user.click(within(grant).getByRole('button', { name: /workspace-owner/ }))
    expect(within(grant).getByRole('radio', { name: /^viewer$/i })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    await user.click(within(grant).getByRole('radio', { name: /^viewer$/i }))
    expect(within(grant).getByRole('radio', { name: /^viewer$/i })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    expect(within(grant).getByText('workspace-owner → viewer')).toBeInTheDocument()
    await user.click(within(grant).getByRole('button', { name: 'Add member' }))

    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          String(input) === '/v1/admin/workspaces/workspace-1/members/user-second' &&
          init?.method === 'PUT',
      ),
    ).toBe(true)
    expect(
      await screen.findByText('workspace-owner added to Launch notes as viewer.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Add a member' })).not.toBeInTheDocument()

    expect(screen.queryByRole('button', { name: 'Show catalogued workspaces' })).not.toBeInTheDocument()
    const navigation = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(within(navigation).getByRole('button', { name: 'All workspaces' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Users' }))
    expect(await screen.findByRole('heading', { name: 'Users', level: 1 })).toBeInTheDocument()
    const directoryTable = screen.getByRole('table', { name: 'User directory' })
    await user.click(
      within(directoryTable).getByRole('button', { name: 'More actions for workspace-owner' }),
    )
    const menu = screen.getByRole('menu', { name: 'Actions for workspace-owner' })
    expect(within(menu).getByText('Reset password')).toBeInTheDocument()
    expect(within(menu).getByText('Delete user')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu', { name: 'Actions for workspace-owner' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(screen.getByRole('button', { name: 'All workspaces' }))
    expect(await screen.findByRole('heading', { name: 'Workspaces', level: 1 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    expect(screen.getByRole('button', { name: 'All workspaces' })).toHaveClass('nav-link-active')
  })

  it('opens the help reference from the rail and moves the active mark to it', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    installApi(owner, [workspace])
    const user = userEvent.setup()
    render(<App />)

    await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    const navigation = screen.getByRole('navigation', { name: 'Main navigation' })
    await user.click(within(navigation).getByRole('button', { name: 'Help' }))

    expect(await screen.findByRole('heading', { name: 'Help', level: 1 })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'The codes' })).toBeInTheDocument()
    expect(screen.getByText("A document's permanent identifier inside this workspace. Cited in answers.")).toBeInTheDocument()
    expect(window.location.search).toContain('view=help')
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    const reopened = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(within(reopened).getByRole('button', { name: 'Help' })).toHaveClass('nav-link-active')
    expect(within(reopened).getByRole('button', { name: 'Documents' })).not.toHaveClass('nav-link-active')
  })

  it('restores the users view for an admin refreshing the dashboard', async () => {
    window.history.replaceState(null, '', '/dashboard?view=users')
    writeSessionToken('admin-token')
    installApi(admin, [
      {
        ...workspace,
        membership_role: null,
        can_upload: false,
        can_download: false,
        can_search_context: false,
        can_manage_members: false,
        can_delete: false,
      },
    ])
    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Users', level: 1 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    const navigation = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(within(navigation).getByRole('button', { name: 'Users' })).toHaveClass('nav-link-active')
    expect(window.location.search).toContain('view=users')
  })

  it('offers reindex to an owner and swaps the row into its new state', async () => {
    window.history.replaceState(null, '', '/dashboard?view=documents')
    writeSessionToken('member-token')
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
    const fetchMock = installApi(owner, [workspace], undefined, {}, {
      list: { items: [document], total: 1, offset: 0, limit: 50 },
    })
    const user = userEvent.setup()

    render(<App />)

    expect(await screen.findByText('launch-review.pdf')).toBeInTheDocument()
    const row = screen.getByRole('row', { name: /launch-review.pdf/ })
    expect(within(row).getByText('Ready')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Reindex launch-review.pdf' }))

    const confirm = await screen.findByRole('dialog', { name: 'Reindex this document?' })
    expect(
      within(confirm).getByText(/replaced once the new run finishes/),
    ).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes('/reindex'))).toHaveLength(0)

    await user.click(within(confirm).getByRole('button', { name: 'Reindex document' }))

    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          String(input) === '/v1/workspaces/workspace-1/documents/document-1/reindex' &&
          init?.method === 'POST',
      ),
    ).toBe(true)
    expect(await screen.findByText('launch-review.pdf queued for reindexing.')).toBeInTheDocument()
    await waitFor(() =>
      expect(
        within(screen.getByRole('row', { name: /launch-review.pdf/ })).getByText('Processing'),
      ).toBeInTheDocument(),
    )
  })

  it('hides reindex from an editor who may delete', async () => {
    window.history.replaceState(null, '', '/dashboard?view=documents')
    writeSessionToken('member-token')
    const editorWorkspace = { ...workspace, membership_role: 'editor' as const }
    installApi(owner, [editorWorkspace], undefined, {}, {
      list: {
        items: [
          {
            id: 'document-2',
            workspace_id: editorWorkspace.id,
            original_filename: 'editable.pdf',
            detected_media_type: 'application/pdf',
            size_bytes: 1024,
            sha256: 'b'.repeat(64),
            status: 'ready',
            created_by_user_id: 'user-owner',
            created_at: '2026-09-24T08:00:00Z',
            duplicate: false,
          },
        ],
        total: 1,
        offset: 0,
        limit: 50,
      },
    })

    render(<App />)

    expect(await screen.findByText('editable.pdf')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reindex editable.pdf' })).not.toBeInTheDocument()
  })

  it('debounces the search so typing quickly costs one request', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    const hit = {
      document_id: 'document-1',
      workspace_id: workspace.id,
      workspace_name: workspace.name,
      filename: 'quarterly-report.pdf',
      media_type: 'application/pdf',
      size_bytes: 2048,
      status: 'ready',
      row_count: 0,
      created_at: '2026-09-24T08:00:00Z',
    }
    const fetchMock = installApi(owner, [workspace], undefined, {}, {
      search: { items: [hit], total: 1 },
    })
    const user = userEvent.setup()

    render(<App />)

    await gotoOverview(user)
    await user.type(screen.getByLabelText('Search documents'), 'quarterly')

    const searchCalls = () =>
      fetchMock.mock.calls.filter(([input]) => String(input).startsWith('/v1/documents/search')).length

    expect(await screen.findByRole('listbox', { name: 'Document search results' })).toBeInTheDocument()
    await waitFor(() => expect(searchCalls()).toBe(1))
    await new Promise((resolve) => setTimeout(resolve, 600))
    expect(searchCalls()).toBe(1)
  })

  it('searches every readable workspace and jumps to the picked document', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    const targetHit = {
      document_id: 'document-target',
      workspace_id: 'workspace-2',
      workspace_name: 'Read only',
      filename: 'shared-notes.pdf',
      media_type: 'application/pdf',
      size_bytes: 2048,
      status: 'ready',
      row_count: 0,
      created_at: '2026-09-24T08:00:00Z',
    }
    const ownHit = { ...targetHit, document_id: 'document-own', workspace_id: 'workspace-1', workspace_name: 'Launch notes', filename: 'launch-review.pdf' }
    installApi(
      owner,
      [workspace, { ...workspace, id: 'workspace-2', name: 'Read only', membership_role: 'viewer', can_upload: false }],
      undefined,
      {},
      { search: { items: [ownHit, targetHit], total: 2 } },
    )
    const user = userEvent.setup()

    render(<App />)

    await gotoOverview(user)
    await user.type(screen.getByLabelText('Search documents'), 'shared')
    const panel = await screen.findByRole('listbox', { name: 'Document search results' })
    expect(await within(panel).findByText('shared-notes.pdf')).toBeInTheDocument()
    expect(within(panel).getByText('Read only')).toBeInTheDocument()
    expect(within(panel).getByText('Launch notes')).toBeInTheDocument()

    await user.click(within(panel).getByRole('option', { name: /shared-notes\.pdf/ }))

    expect(await screen.findByRole('heading', { name: 'Read only', level: 1 })).toBeInTheDocument()
    expect(window.location.search).toContain('ws=workspace-2')
    expect(window.location.search).toContain('view=documents')
  })

  it('restores the documents view and workspace for a member refreshing the dashboard', async () => {
    window.history.replaceState(null, '', `/dashboard?view=documents&ws=${workspace.id}`)
    writeSessionToken('member-token')
    installApi(owner, [workspace])
    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Launch notes', level: 1 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    const navigation = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(within(navigation).getByRole('button', { name: 'Documents' })).toHaveClass('nav-link-active')
    expect(window.location.search).toContain('view=documents')
    expect(window.location.search).toContain(`ws=${workspace.id}`)
  })

  it('keeps the documents view when switching workspaces', async () => {
    window.history.replaceState(null, '', `/dashboard?view=documents&ws=${workspace.id}`)
    writeSessionToken('member-token')
    installApi(owner, [
      workspace,
      {
        ...workspace,
        id: 'workspace-2',
        name: 'Read only',
        membership_role: 'viewer',
        can_upload: false,
      },
    ])
    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Launch notes', level: 1 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(
      screen.getByRole('button', { name: 'Current workspace Launch notes, switch workspace' }),
    )
    await user.click(screen.getByRole('menuitemradio', { name: 'Switch to Read only' }))
    expect(await screen.findByRole('heading', { name: 'Read only', level: 1 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    const navigation = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(within(navigation).getByRole('button', { name: 'Documents' })).toHaveClass('nav-link-active')
    expect(window.location.search).toContain('ws=workspace-2')
  })

  it('stays on the upload page and follows the picked workspace', async () => {
    window.history.replaceState(null, '', '/upload-context')
    writeSessionToken('member-token')
    installApi(owner, [
      workspace,
      {
        ...workspace,
        id: 'workspace-2',
        name: 'Second space',
        membership_role: 'editor',
      },
    ])
    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Upload context', level: 1 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(
      screen.getByRole('button', { name: 'Current workspace Launch notes, switch workspace' }),
    )
    await user.click(screen.getByRole('menuitemradio', { name: 'Switch to Second space' }))
    expect(screen.getByRole('heading', { name: 'Upload context', level: 1 })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Workspace' })).toHaveValue('workspace-2')
    expect(window.location.pathname).toBe('/upload-context')
  })

  it('changes a member role through the edit dialog with an explicit save', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    const fetchMock = installApi(owner, [workspace], undefined, {}, {}, {
      list: {
        items: [
          {
            user_id: 'member-9',
            username: 'nada',
            role: 'viewer',
            granted_by_user_id: owner.id,
            created_at: '2026-09-23T08:00:00Z',
            updated_at: '2026-09-23T08:00:00Z',
          },
        ],
      },
    })
    const user = userEvent.setup()
    render(<App />)

    expect(await gotoOverview(user)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Manage access' }))
    expect(await screen.findByText('nada')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Change access for nada' })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: 'Change access for nada' }))
    const dialog = screen.getByRole('dialog', { name: 'Change access' })
    expect(within(dialog).getByRole('radio', { name: /^viewer$/i })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    await user.click(within(dialog).getByRole('radio', { name: /^editor$/i }))
    await user.click(within(dialog).getByRole('button', { name: 'Save access' }))

    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          String(input) === '/v1/workspaces/workspace-1/members/member-9' && init?.method === 'PUT',
      ),
    ).toBe(true)
    expect(await screen.findByText('nada is now editor.')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Change access' })).not.toBeInTheDocument()
    const memberRow = screen.getByText('nada').closest('tr')
    if (memberRow === null) throw new Error('member row missing')
    expect(within(memberRow as HTMLElement).getByText('editor')).toBeInTheDocument()
  })

  it('ignores an admin-only view in a deep link for a regular user', async () => {
    window.history.replaceState(null, '', `/dashboard?view=users&ws=${workspace.id}`)
    writeSessionToken('member-token')
    installApi(owner, [workspace])
    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Chat', level: 1 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    const navigation = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(within(navigation).queryByRole('button', { name: 'Users' })).not.toBeInTheDocument()
    expect(window.location.search).toContain('view=chat')
  })

  it('streams an agent answer into the chat conversation', async () => {
    window.history.replaceState(null, '', '/dashboard?view=chat')
    const fetchMock = installApi()
    const user = userEvent.setup()
    writeSessionToken('member-token')
    render(<App />)

    const input = await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.type(input, 'What is in this workspace?')
    await user.keyboard('{Enter}')

    expect(await screen.findByText('What is in this workspace?')).toBeInTheDocument()
    const pinned = screen.getByPlaceholderText('Ask across your workspaces…')
    expect(pinned).not.toBe(input)
    expect(await screen.findByText('search_context')).toBeInTheDocument()
    expect(await screen.findByText('The workspace holds one report.')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Findings' })).toBeInTheDocument()
    const transcript = screen.getByTestId('chat-transcript')
    expect(within(transcript).getByRole('list')).toBeInTheDocument()
    expect(within(transcript).getByText('Revenue rose by a tenth.')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument(),
    )
    const chatCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith('/agent/chat'),
    )
    if (chatCall === undefined) throw new Error('Expected an agent chat request')
    const headers = chatCall[1]?.headers
    if (!(headers instanceof Headers)) throw new Error('Expected request headers')
    expect(headers.get('Authorization')).toBe('Bearer member-token')
  })

  it('shows every tool call, not only the first', async () => {
    window.history.replaceState(null, '', '/dashboard?view=chat')
    installApi(admin, [workspace], undefined, {}, {}, {}, {}, {
      toolCalls: [
        { name: 'find_documents', arguments: { pattern: '3M' }, preview: 'found the 10-K' },
        {
          name: 'read_document',
          arguments: { document_id: 'doc-1', workspace_id: workspace.id },
          preview: 'executive officers',
        },
      ],
    })
    const user = userEvent.setup()
    writeSessionToken('member-token')
    render(<App />)

    const input = await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.type(input, 'who is the executive officer of 3M?')
    await user.keyboard('{Enter}')

    expect(await screen.findByText('read_document')).toBeInTheDocument()
    expect(screen.getByText('2 tool calls')).toBeInTheDocument()
    expect(screen.queryByText('find_documents')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /2 tool calls/ }))
    expect(await screen.findByText('find_documents')).toBeInTheDocument()
  })

  it('does not abort the first message of a new chat while the draft promotes', async () => {
    window.history.replaceState(null, '', '/dashboard?view=chat')
    installApi(admin, [workspace], undefined, {}, {}, {}, {}, { holdBeforeFinalMs: 500 })
    const user = userEvent.setup()
    writeSessionToken('member-token')
    render(<App />)

    const input = await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.type(input, 'What is in this workspace?')
    await user.keyboard('{Enter}')

    // the debounced save promotes the draft into a stored session 300ms into
    // the request; the promotion must not kill the stream it just started
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 450))
    })
    expect(await screen.findByText('The workspace holds one report.')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument(),
    )
  })

  it('keeps the conversation when leaving the chat view and coming back', async () => {
    window.history.replaceState(null, '', '/dashboard?view=chat')
    installApi()
    const user = userEvent.setup()
    writeSessionToken('member-token')
    render(<App />)

    const input = await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.type(input, 'What is in this workspace?')
    await user.keyboard('{Enter}')
    await screen.findByText('The workspace holds one report.')

    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(
      within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('button', {
        name: 'Overview',
      }),
    )
    expect(screen.queryByText('The workspace holds one report.')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(
      within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('button', {
        name: 'Chat',
      }),
    )
    expect(await screen.findByText('The workspace holds one report.')).toBeInTheDocument()
    expect(screen.getByText('What is in this workspace?')).toBeInTheDocument()
  })

  it('keeps the conversation when the workspace changes', async () => {
    const other: Workspace = {
      ...workspace,
      id: 'workspace-2',
      slug: 'other-notes',
      name: 'Other notes',
      membership_role: 'editor' as const,
    }
    window.history.replaceState(null, '', '/dashboard?view=chat')
    installApi(owner, [workspace, other])
    writeSessionToken('member-token')
    const user = userEvent.setup()
    render(<App />)

    const input = await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.type(input, 'What is in this workspace?')
    await user.keyboard('{Enter}')
    await screen.findByText('The workspace holds one report.')

    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(
      screen.getByRole('button', { name: 'Current workspace Launch notes, switch workspace' }),
    )
    await user.click(screen.getByRole('menuitemradio', { name: 'Switch to Other notes' }))
    const close = screen.queryByRole('button', { name: 'Close navigation drawer' })
    if (close !== null) await user.click(close)

    expect(await screen.findByText('The workspace holds one report.')).toBeInTheDocument()
    expect(screen.getByText('What is in this workspace?')).toBeInTheDocument()
  })

  it('keeps the chat nav out of admin mode', async () => {
    window.history.replaceState(null, '', '/')
    installApi(admin, [])
    const user = userEvent.setup()
    writeSessionToken('member-token')
    render(<App />)

    expect(await screen.findByRole('button', { name: 'Open navigation' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    const navigation = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(within(navigation).queryByRole('button', { name: 'Chat' })).not.toBeInTheDocument()
    expect(window.location.search).toContain('view=admin-overview')
  })

  it('restores the conversation from browser storage after a refresh', async () => {
    window.history.replaceState(null, '', '/dashboard?view=chat')
    installApi()
    writeSessionToken('member-token')
    const user = userEvent.setup()
    const view = render(<App />)

    const input = await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.type(input, 'What is in this workspace?')
    await user.keyboard('{Enter}')
    await screen.findByText('The workspace holds one report.')
    // the debounced save fires 300ms after the last change
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 450))
    })
    view.unmount()

    writeSessionToken('member-token')
    render(<App />)
    expect(
      await screen.findByText('The workspace holds one report.'),
    ).toBeInTheDocument()
    expect(screen.getAllByText('What is in this workspace?').length).toBeGreaterThan(0)
    expect(
      screen.getByRole('button', { name: 'Start a new conversation' }),
    ).toBeInTheDocument()
  })

  it('starts a fresh conversation when New chat is pressed', async () => {
    window.history.replaceState(null, '', '/dashboard?view=chat')
    installApi()
    writeSessionToken('member-token')
    const user = userEvent.setup()
    render(<App />)

    const input = await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.type(input, 'What is in this workspace?')
    await user.keyboard('{Enter}')
    await screen.findByText('The workspace holds one report.')

    await user.click(screen.getByRole('button', { name: 'Start a new conversation' }))

    expect(
      await screen.findByPlaceholderText('Ask across your workspaces…'),
    ).toBeInTheDocument()
    const heading = screen.getByText('Ask across your workspaces')
    expect(heading).toBeInTheDocument()
    expect(screen.queryByText('The workspace holds one report.')).not.toBeInTheDocument()
  })

  it('wipes the stored chat on sign out', async () => {
    window.history.replaceState(null, '', '/dashboard?view=chat')
    installApi()
    writeSessionToken('member-token')
    const user = userEvent.setup()
    render(<App />)

    const input = await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.type(input, 'What is in this workspace?')
    await user.keyboard('{Enter}')
    await screen.findByText('The workspace holds one report.')

    localStorage.removeItem('retriv.chat.conversations.v2')
    expect(loadChatHistory().conversations).toEqual([])
  })

  it('lists past sessions in the sidebar and restores the one picked', async () => {
    window.history.replaceState(null, '', '/dashboard?view=chat')
    installApi()
    writeSessionToken('member-token')
    const user = userEvent.setup()
    render(<App />)

    const input = await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.type(input, 'first conversation question')
    await user.keyboard('{Enter}')
    await screen.findByText('The workspace holds one report.')

    await user.click(screen.getByRole('button', { name: 'Start a new conversation' }))
    const fresh = await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.type(fresh, 'second conversation question')
    await user.keyboard('{Enter}')
    expect(await screen.findByText('The workspace holds one report.')).toBeInTheDocument()
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 450))
    })

    const sidebar = screen.getByRole('list', { name: 'Chat sessions' })
    expect(within(sidebar).getByText('first conversation question')).toBeInTheDocument()
    expect(within(sidebar).getByText('second conversation question')).toBeInTheDocument()

    await user.click(within(sidebar).getByText('first conversation question'))
    expect(await screen.findByText('The workspace holds one report.')).toBeInTheDocument()
    expect(screen.getAllByText('first conversation question').length).toBeGreaterThan(0)
  })

  it('deletes a session from the sidebar and clears it when active', async () => {
    window.history.replaceState(null, '', '/dashboard?view=chat')
    installApi()
    writeSessionToken('member-token')
    const user = userEvent.setup()
    render(<App />)

    const input = await screen.findByPlaceholderText('Ask across your workspaces…')
    await user.type(input, 'first conversation question')
    await user.keyboard('{Enter}')
    await screen.findByText('The workspace holds one report.')
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 450))
    })

    await user.click(
      screen.getByRole('button', { name: 'Delete conversation: first conversation question' }),
    )

    expect(await screen.findByText('Ask across your workspaces')).toBeInTheDocument()
    expect(screen.queryByText('The workspace holds one report.')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Delete conversation: first conversation question' }),
    ).not.toBeInTheDocument()
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

  it('renews an expired access token instead of signing the user out', async () => {
    writeSession({ accessToken: 'stale-token', refreshToken: `rt_${'z'.repeat(43)}` })
    const fetchMock = installApi(owner, [workspace], undefined, {}, {}, {}, {
      meFirst: 'expired',
    })

    render(<App />)

    expect(await screen.findByRole('button', { name: 'Open navigation' })).toBeInTheDocument()
    expect(fetchMock.refreshes).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()

    const meCalls = fetchMock.mock.calls.filter(([input]) => String(input) === '/v1/auth/me')
    expect(meCalls).toHaveLength(2)
    const retried = new Headers(meCalls[1][1]?.headers)
    expect(retried.get('Authorization')).toBe('Bearer refreshed-token')
    expect(readSession()?.accessToken).toBe('refreshed-token')
  })

  it('shares one renewal between requests refused at the same time', async () => {
    writeSession({ accessToken: 'stale-token', refreshToken: `rt_${'y'.repeat(43)}` })
    const fetchMock = installApi(owner, [workspace], undefined, {}, {}, {}, {
      meFirst: 'expired',
    })

    render(<App />)

    await screen.findByRole('button', { name: 'Open navigation' })
    const workspaceCalls = fetchMock.mock.calls.filter(([input]) =>
      String(input).startsWith('/v1/workspaces?'),
    )
    const staleCalls = workspaceCalls.filter(
      ([, init]) => new Headers(init?.headers).get('Authorization') === 'Bearer stale-token',
    )
    expect(staleCalls.length).toBeGreaterThan(0)
    expect(fetchMock.refreshes).toHaveLength(1)
  })

  it('returns to sign in when the session cannot be renewed', async () => {
    writeSession({ accessToken: 'stale-token', refreshToken: `rt_${'x'.repeat(43)}` })
    installApi(owner, [workspace], undefined, {}, {}, {}, { meFirst: 'expired', refresh: 'rejected' })

    render(<App />)

    expect(await screen.findByRole('button', { name: 'Continue' })).toBeInTheDocument()
    expect(readSessionToken()).toBeNull()
  })

  it('revokes the session on the server when signing out', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSession({ accessToken: 'member-token', refreshToken: `rt_${'w'.repeat(43)}` })
    const fetchMock = installApi(owner, [workspace])
    const user = userEvent.setup()

    render(<App />)

    await gotoOverview(user)
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(screen.getByRole('button', { name: 'Sign out' }))
    const dialog = await screen.findByRole('dialog', { name: 'Sign out of Retriv?' })
    await user.click(within(dialog).getByRole('button', { name: 'Sign out' }))

    await waitFor(() => expect(readSessionToken()).toBeNull())
    expect(await screen.findByRole('button', { name: 'Continue' })).toBeInTheDocument()
    expect(fetchMock.logouts).toHaveLength(1)
    expect(JSON.parse(fetchMock.logouts[0])).toEqual({ refresh_token: `rt_${'w'.repeat(43)}` })
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
    const user = userEvent.setup()
    const fetchMock = installApi(owner, [workspace])

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

  it('accepts a workbook, which is stored as one table per sheet', async () => {
    const user = userEvent.setup()
    const fetchMock = installApi(owner, [workspace])

    window.history.pushState({}, '', '/upload-context')
    render(<App />)
    await user.type(screen.getByLabelText('Username'), 'maya')
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByRole('heading', { name: 'Upload context', level: 1 })).toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Workspace' }), workspace.id)

    const file = new File(['x'], 'budget.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    await user.upload(screen.getByLabelText('Document'), file)
    expect(screen.getByRole('button', { name: 'Upload document' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: 'Upload document' }))

    expect(await screen.findByRole('heading', { name: 'Document accepted' })).toBeInTheDocument()
    const uploadCall = fetchMock.mock.calls.find(([input]) => String(input) === '/v1/workspaces/workspace-1/documents')
    if (uploadCall === undefined) throw new Error('Expected an upload request')
    const body = uploadCall[1]?.body
    if (!(body instanceof FormData)) throw new Error('Expected multipart form data')
    expect((body.get('file') as File).name).toBe('budget.xlsx')
  })

  it('accepts a presentation, which is rendered before it is read', async () => {
    const user = userEvent.setup()
    installApi(owner, [workspace])

    window.history.pushState({}, '', '/upload-context')
    render(<App />)
    await user.type(screen.getByLabelText('Username'), 'maya')
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByRole('heading', { name: 'Upload context', level: 1 })).toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Workspace' }), workspace.id)

    const file = new File(['x'], 'deck.pptx', {
      type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    })
    await user.upload(screen.getByLabelText('Document'), file)
    expect(screen.getByRole('button', { name: 'Upload document' })).toBeEnabled()
  })

  it('rejects a legacy binary spreadsheet the pipeline cannot read', async () => {
    const user = userEvent.setup()
    installApi(owner, [workspace])

    window.history.pushState({}, '', '/upload-context')
    render(<App />)
    await user.type(screen.getByLabelText('Username'), 'maya')
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByRole('heading', { name: 'Upload context', level: 1 })).toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Workspace' }), workspace.id)

    fireEvent.change(screen.getByLabelText('Document'), {
      target: { files: [new File(['x'], 'budget.xls', { type: 'application/vnd.ms-excel' })] },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent('Only PDF, DOCX, PPTX, XLSX and CSV')
    expect(screen.getByRole('button', { name: 'Upload document' })).toBeDisabled()
  })

  it('shows how many rows a CSV holds, and says plainly how a CSV is used', async () => {
    window.history.replaceState(null, '', '/dashboard?view=documents')
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

    render(<App />)

    expect(await screen.findByText('sales.csv')).toBeInTheDocument()
    expect(screen.getByText(/900 rows/)).toBeInTheDocument()
    expect(screen.getByText('UNDP.pdf')).toBeInTheDocument()
    expect(screen.getAllByText(/\b900 rows\b/)).toHaveLength(1)
    expect(screen.queryByText(/\b0 rows\b/)).not.toBeInTheDocument()
  })

  it('no longer tells the user spreadsheets are unsupported', async () => {
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
    const help = screen.getByText(/PDF, DOCX, PPTX, XLSX or CSV/)
    expect(help).toBeInTheDocument()
    expect(help).toHaveTextContent(/queried with SQL/)
    expect(help).toHaveTextContent(/each sheet as its own table/)
    expect(screen.queryByText(/not yet indexed/)).not.toBeInTheDocument()
  })

  it('navigates to the upload route from the dashboard and follows browser history', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    installApi(owner)
    const user = userEvent.setup()
    render(<App />)

    expect(await gotoOverview(user)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(screen.getByRole('link', { name: 'Upload context' }))
    expect(window.location.pathname).toBe('/upload-context')
    expect(screen.getByRole('heading', { name: 'Upload context', level: 1 })).toBeInTheDocument()
    window.history.replaceState(null, '', '/dashboard')
    window.dispatchEvent(new PopStateEvent('popstate'))
    expect(await screen.findByRole('heading', { name: 'Launch notes', level: 1 })).toBeInTheDocument()
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

  it('shows the in-flight processes in the notifications popover', async () => {
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
    installApi(owner, [workspace], undefined, {
      runs: { items: [activeRun], total: 1 },
    })
    const user = userEvent.setup()

    render(<App />)

    await gotoOverview(user)
    const notificationButton = await screen.findByRole('button', {
      name: 'Open processing notifications, 1 active run',
    })
    await user.click(notificationButton)

    const panel = screen.getByRole('dialog', { name: 'Processing activity' })
    expect(within(panel).getByText('report-processing.pdf')).toBeInTheDocument()
    expect(within(panel).getByText('2/4 embeddings')).toBeInTheDocument()
    expect(within(panel).getByRole('button', { name: 'Refresh processing activity' })).toBeEnabled()
    expect(
      within(panel).queryByText('No unfinished processes across your workspaces.'),
    ).not.toBeInTheDocument()
  })

  it('stops requesting processing runs once loaded instead of polling', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      window.history.replaceState(null, '', '/dashboard')
      writeSessionToken('member-token')
      const fetchMock = installApi(owner, [workspace])
      const countProcessingRequests = () =>
        fetchMock.mock.calls.filter(([input]) => String(input) === '/v1/processing-runs').length

      render(<App />)

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
      status: 'embedding',
      attempts: 1,
      chunk_count: 0,
      embedded_chunk_count: 0,
      created_at: '2026-09-24T08:00:00Z',
      started_at: '2026-09-24T08:00:01Z',
      completed_at: null,
    }
    const processingResponses = {
      runs: { items: [firstRun], total: 1 },
    }
    installApi(owner, [workspace], undefined, processingResponses)
    const user = userEvent.setup()

    render(<App />)

    await user.click(await screen.findByRole('button', {
      name: 'Open processing notifications, 1 active run',
    }))
    expect((await screen.findAllByText('first-upload.pdf')).length).toBeGreaterThanOrEqual(1)

    processingResponses.runs = {
      items: [{
        ...firstRun,
        id: 'run-second',
        document_id: 'document-second',
        filename: 'second-upload.pdf',
      }],
      total: 1,
    }

    await user.click(screen.getByRole('button', { name: 'Refresh processing activity' }))

    expect((await screen.findAllByText('second-upload.pdf')).length).toBeGreaterThanOrEqual(1)
    expect(screen.queryByText('first-upload.pdf')).not.toBeInTheDocument()
  })

  it('opens a document in the viewer and reuses the cached bytes', async () => {
    window.history.replaceState(null, '', '/dashboard?view=documents')
    writeSessionToken('member-token')
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
    const fetchMock = installApi(owner, [workspace], undefined, {}, {
      list: { items: [document], total: 1, offset: 0, limit: 50 },
    })
    const user = userEvent.setup()

    render(<App />)

    expect(await screen.findByText('launch-review.pdf')).toBeInTheDocument()

    const downloadCalls = () =>
      fetchMock.mock.calls.filter(([input]) => String(input).includes('/download')).length

    await user.click(screen.getByRole('button', { name: 'View launch-review.pdf' }))
    const viewer = await screen.findByRole('dialog', { name: 'launch-review.pdf' })
    await waitFor(() => expect(downloadCalls()).toBe(1))

    await user.click(within(viewer).getByRole('button', { name: 'Close launch-review.pdf' }))
    expect(screen.queryByRole('dialog', { name: 'launch-review.pdf' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'View launch-review.pdf' }))
    expect(await screen.findByRole('dialog', { name: 'launch-review.pdf' })).toBeInTheDocument()
    await waitFor(() => expect(downloadCalls()).toBe(1))
  })

  it('shows the error instead of a blank viewer when the download is refused', async () => {
    window.history.replaceState(null, '', '/dashboard?view=documents')
    writeSessionToken('member-token')
    installApi(owner, [workspace], undefined, {}, {
      list: {
        items: [
          {
            id: 'document-1',
            workspace_id: workspace.id,
            original_filename: 'launch-review.pdf',
            detected_media_type: 'application/pdf',
            size_bytes: 2048,
            sha256: 'b'.repeat(64),
            status: 'ready',
            created_by_user_id: 'user-owner',
            created_at: '2026-09-24T08:00:00Z',
            duplicate: false,
          },
        ],
        total: 1,
        offset: 0,
        limit: 50,
      },
      download: 'forbidden',
    })
    const user = userEvent.setup()

    render(<App />)

    expect(await screen.findByText('launch-review.pdf')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'View launch-review.pdf' }))

    const viewer = await screen.findByRole('dialog', { name: 'launch-review.pdf' })
    expect(await within(viewer).findByRole('alert')).toBeInTheDocument()
    expect(within(viewer).queryByTitle('launch-review.pdf')).not.toBeInTheDocument()
  })

  it('lays out a Word document instead of refusing to preview it', async () => {
    window.history.replaceState(null, '', '/dashboard?view=documents')
    writeSessionToken('member-token')
    const docx = await buildDocx('Business Case Answer Sheet')
    installApi(owner, [workspace], undefined, {}, {
      list: {
        items: [
          {
            id: 'document-docx',
            workspace_id: workspace.id,
            original_filename: 'business-case.docx',
            detected_media_type: DOCX_MEDIA_TYPE,
            size_bytes: docx.size,
            sha256: 'c'.repeat(64),
            status: 'ready',
            created_by_user_id: 'user-owner',
            created_at: '2026-09-24T08:00:00Z',
            duplicate: false,
          },
        ],
        total: 1,
        offset: 0,
        limit: 50,
      },
      downloadBlob: docx,
    })
    const user = userEvent.setup()

    render(<App />)

    expect(await screen.findByText('business-case.docx')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'View business-case.docx' }))

    const viewer = await screen.findByRole('dialog', { name: 'business-case.docx' })
    const preview = await within(viewer).findByTestId('docx-preview', {}, { timeout: 5000 })
    expect(within(viewer).queryByText('No inline preview for this file type')).not.toBeInTheDocument()

    await waitFor(() =>
      expect(preview.shadowRoot?.textContent).toContain('Business Case Answer Sheet'),
    )
  })

  it('fetches a Word document once across repeated previews', async () => {
    window.history.replaceState(null, '', '/dashboard?view=documents')
    writeSessionToken('member-token')
    const docx = await buildDocx('Repeat preview')
    const fetchMock = installApi(owner, [workspace], undefined, {}, {
      list: {
        items: [
          {
            id: 'document-docx-2',
            workspace_id: workspace.id,
            original_filename: 'repeat.docx',
            detected_media_type: DOCX_MEDIA_TYPE,
            size_bytes: docx.size,
            sha256: 'd'.repeat(64),
            status: 'ready',
            created_by_user_id: 'user-owner',
            created_at: '2026-09-24T08:00:00Z',
            duplicate: false,
          },
        ],
        total: 1,
        offset: 0,
        limit: 50,
      },
      downloadBlob: docx,
    })
    const user = userEvent.setup()

    const downloadCalls = () =>
      fetchMock.mock.calls.filter(([input]) => String(input).includes('/download')).length

    render(<App />)

    expect(await screen.findByText('repeat.docx')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'View repeat.docx' }))
    const first = await screen.findByRole('dialog', { name: 'repeat.docx' })
    await within(first).findByTestId('docx-preview', {}, { timeout: 5000 })
    await waitFor(() => expect(downloadCalls()).toBe(1))

    await user.click(within(first).getByRole('button', { name: 'Close repeat.docx' }))
    await user.click(screen.getByRole('button', { name: 'View repeat.docx' }))

    const second = await screen.findByRole('dialog', { name: 'repeat.docx' })
    await within(second).findByTestId('docx-preview')
    expect(downloadCalls()).toBe(1)
  })

  it('follows the system theme by default', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    installApi(owner, [workspace])
    const user = userEvent.setup()

    render(<App />)

    await gotoOverview(user)
    expect(document.documentElement.dataset.theme).toBe('light')
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(screen.getByRole('button', { name: /^Theme: system/ }))

    const menu = screen.getByRole('menu', { name: 'Theme' })
    expect(within(menu).getByRole('menuitemradio', { name: 'System' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
  })

  it('applies and remembers an explicit dark choice', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    installApi(owner, [workspace])
    const user = userEvent.setup()

    render(<App />)

    await gotoOverview(user)
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(screen.getByRole('button', { name: /^Theme:/ }))
    await user.click(screen.getByRole('menuitemradio', { name: 'Dark' }))

    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(document.documentElement.style.colorScheme).toBe('dark')
    expect(localStorage.getItem('retriv.theme')).toBe('dark')
  })

  it('keeps the explicit choice when the system says otherwise', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: query.includes('prefers-color-scheme: dark'),
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }))
    installApi(owner, [workspace])
    const user = userEvent.setup()

    render(<App />)

    await gotoOverview(user)
    expect(document.documentElement.dataset.theme).toBe('dark')

    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(screen.getByRole('button', { name: /^Theme:/ }))
    await user.click(screen.getByRole('menuitemradio', { name: 'Light' }))

    expect(document.documentElement.dataset.theme).toBe('light')
    expect(localStorage.getItem('retriv.theme')).toBe('light')
  })

  it('stays on the api keys view when the workspace is switched', async () => {
    window.history.replaceState(null, '', '/dashboard?view=api-keys')
    writeSessionToken('member-token')
    installApi(owner, [
      workspace,
      { ...workspace, id: 'workspace-2', name: 'Read only', membership_role: 'viewer' },
    ])
    const user = userEvent.setup()

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'API keys', level: 1 })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(screen.getByRole('button', { name: /switch workspace/ }))
    await user.click(screen.getByRole('menuitemradio', { name: 'Switch to Read only' }))

    expect(await screen.findByRole('heading', { name: 'API keys', level: 1 })).toBeInTheDocument()
    expect(window.location.search).toContain('view=api-keys')
    expect(window.location.search).toContain('ws=workspace-2')
  })

  it('asks before signing out and only ends the session on confirm', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    installApi(owner)
    const user = userEvent.setup()

    render(<App />)

    await gotoOverview(user)
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    const dialog = screen.getByRole('dialog', { name: 'Sign out of Retriv?' })
    expect(dialog).toBeInTheDocument()
    expect(
      within(dialog).getByText(/need to sign in again to reach the dashboard/),
    ).toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog', { name: 'Sign out of Retriv?' })).not.toBeInTheDocument()
    expect(readSessionToken()).toBe('member-token')
    expect(window.location.pathname).toBe('/dashboard')

    await user.click(screen.getByRole('button', { name: 'Sign out' }))
    await user.click(
      within(screen.getByRole('dialog', { name: 'Sign out of Retriv?' })).getByRole('button', {
        name: 'Sign out',
      }),
    )
    await waitFor(() => expect(readSessionToken()).toBeNull())
    expect(await screen.findByRole('button', { name: 'Continue' })).toBeInTheDocument()
  })

  it('closes the slide-in navigation with Escape and the backdrop', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    installApi(owner)
    const user = userEvent.setup()

    render(<App />)

    await gotoOverview(user)
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
    window.history.replaceState(null, '', '/dashboard?view=documents')
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

    expect(await screen.findByText('launch-review.pdf')).toBeInTheDocument()
    const documentRow = screen.getByRole('row', { name: /launch-review.pdf/ })
    expect(within(documentRow).getByText('Ready')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Delete launch-review.pdf' }))
    expect(screen.getByRole('dialog', { name: /Delete this document/ })).toBeInTheDocument()

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

    render(<App />)
    const user = userEvent.setup()
    expect(await gotoOverview(user, 'Shared reference')).toBeInTheDocument()

    expect(screen.queryByRole('button', { name: 'Delete shared-notes.pdf' })).toBeNull()
  })
})
