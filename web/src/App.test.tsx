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
  member_count: 3,
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
  processingResponses: { runs?: unknown } = {},
  documents: { list?: unknown; deleted?: string[] } = {},
  members: { list?: unknown } = {},
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
    const documentDelete = url.match(/^\/v1\/workspaces\/[^/]+\/documents\/([^/?]+)$/)
    if (documentDelete !== null && init?.method === 'DELETE') {
      documents.deleted?.push(documentDelete[1])
      if (documents.list !== undefined) {
        const list = documents.list as { items: Array<{ id: string }> }
        list.items = list.items.filter((item) => item.id !== documentDelete[1])
      }
      return new Response(null, { status: 204 })
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

    expect(await screen.findByRole('heading', { name: 'Launch notes', level: 1 })).toBeInTheDocument()
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

    await screen.findByRole('heading', { name: 'Launch notes', level: 1 })
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
        can_search_context: false,
        can_manage_members: false,
        can_delete: false,
      },
    ])

    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Launch notes', level: 1 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    expect(screen.queryByRole('button', { name: 'New workspace' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Users' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open Shared reference workspace' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open Company catalog workspace' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show catalogued workspaces' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Close navigation drawer' }))
    await user.click(screen.getByRole('button', { name: 'Manage access' }))
    expect(await screen.findByRole('heading', { name: 'Workspace members' })).toBeInTheDocument()
  })

  it('expands catalogued workspaces inline without granting access', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    installApi(owner, [
      workspace,
      {
        ...workspace,
        id: 'workspace-3',
        slug: 'company-catalog',
        name: 'Company catalog',
        membership_role: null,
        member_count: 7,
        can_upload: false,
        can_search_context: false,
        can_manage_members: false,
        can_delete: false,
      },
    ])

    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Launch notes', level: 1 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))

    const expander = screen.getByRole('button', { name: 'Show catalogued workspaces' })
    expect(expander).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('button', { name: 'Open Company catalog workspace' })).not.toBeInTheDocument()

    await user.click(expander)
    expect(screen.getByRole('button', { name: 'Collapse catalogued workspaces' })).toHaveAttribute('aria-expanded', 'true')
    const catalogRow = screen.getByRole('button', { name: 'Open Company catalog workspace' })
    expect(within(catalogRow).getByText('Company catalog')).toBeInTheDocument()

    await user.click(catalogRow)
    expect(await screen.findByRole('heading', { name: 'Company catalog', level: 1 })).toBeInTheDocument()
    expect(screen.getByText('Documents are members-only')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(screen.getByRole('button', { name: 'Collapse catalogued workspaces' }))
    expect(screen.queryByRole('button', { name: 'Open Company catalog workspace' })).not.toBeInTheDocument()
  })

  it('gives an admin without memberships a platform overview instead of workspace navigation', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('admin-token')
    const fetchMock = installApi(admin, [
      {
        ...workspace,
        membership_role: null,
        can_upload: false,
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
    expect(within(navigation).getByText('Launch notes')).toBeInTheDocument()

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

  it('restores the users view for an admin refreshing the dashboard', async () => {
    window.history.replaceState(null, '', '/dashboard?view=users')
    writeSessionToken('admin-token')
    installApi(admin, [
      {
        ...workspace,
        membership_role: null,
        can_upload: false,
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
      within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('button', {
        name: 'Open Read only workspace',
      }),
    )
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
      within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('button', {
        name: 'Open Second space workspace',
      }),
    )
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

    expect(await screen.findByRole('heading', { name: 'Launch notes', level: 1 })).toBeInTheDocument()
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
    const memberRow = screen.getByText('nada').closest('li')
    if (memberRow === null) throw new Error('member row missing')
    expect(within(memberRow as HTMLElement).getByText('editor')).toBeInTheDocument()
  })

  it('ignores an admin-only view in a deep link for a regular user', async () => {
    window.history.replaceState(null, '', `/dashboard?view=users&ws=${workspace.id}`)
    writeSessionToken('member-token')
    installApi(owner, [workspace])
    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Launch notes', level: 1 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    const navigation = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(within(navigation).queryByRole('button', { name: 'Users' })).not.toBeInTheDocument()
    expect(window.location.search).toContain('view=overview')
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

  it('rejects a spreadsheet extension the pipeline cannot read', async () => {
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
      target: { files: [new File(['x'], 'budget.xlsx', { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })] },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent('Only PDF, DOCX and CSV')
    expect(screen.getByRole('button', { name: 'Upload document' })).toBeDisabled()
  })

  it('shows how many rows a CSV holds, and says plainly how a CSV is used', async () => {
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

    expect(await screen.findByRole('heading', { name: 'Launch notes', level: 1 })).toBeInTheDocument()
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

    await screen.findByRole('heading', { name: 'Launch notes', level: 1 })
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

  it('closes the slide-in navigation with Escape and the backdrop', async () => {
    window.history.replaceState(null, '', '/dashboard')
    writeSessionToken('member-token')
    installApi(owner)
    const user = userEvent.setup()

    render(<App />)

    await screen.findByRole('heading', { name: 'Launch notes', level: 1 })
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

    expect(await screen.findByText('shared-notes.pdf')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete shared-notes.pdf' })).toBeNull()
  })
})
