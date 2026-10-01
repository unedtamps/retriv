import { act, render, screen, waitFor, within } from '@testing-library/react'
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
  can_download: true,
  can_search_context: true,
  can_manage_members: true,
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
  answerResponse?: Response,
  processingResponses: { active?: unknown; recent?: unknown } = {},
) {
  const conversations: Array<{ id: string; title: string | null; created_at: string; updated_at: string }> = []
  const turns = new Map<string, Array<Record<string, unknown>>>()
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
    if (url.startsWith('/v1/agent/conversations?')) {
      return json({ items: conversations, total: conversations.length, offset: 0, limit: 50 })
    }
    if (url === '/v1/agent/conversations' && init?.method === 'POST') {
      const conversation = {
        id: `conversation-${conversations.length + 1}`,
        title: null,
        created_at: '2026-09-24T08:00:00Z',
        updated_at: '2026-09-24T08:00:00Z',
      }
      conversations.unshift(conversation)
      turns.set(conversation.id, [])
      return json(conversation, 201)
    }
    const conversationTurnsPath = url.match(/^\/v1\/agent\/conversations\/([^/?]+)\/turns/)
    if (conversationTurnsPath !== null && (init?.method === undefined || init.method === 'GET')) {
      const items = turns.get(conversationTurnsPath[1]) ?? []
      return json({ items, total: items.length, offset: 0, limit: 100 })
    }
    if (conversationTurnsPath !== null && init?.method === 'POST') {
      const request = JSON.parse(String(init.body)) as { question: string; request_id: string }
      const responseTurn = answerResponse === undefined
        ? {
            question: request.question,
            answer: 'Revenue increased. [1]',
            status: 'completed',
            failure_code: null,
            created_at: '2026-09-24T08:00:00Z',
            completed_at: '2026-09-24T08:00:01Z',
            context_available: true,
            sources: [],
          }
        : await answerResponse.clone().json()
      const conversationTurns = turns.get(conversationTurnsPath[1]) ?? []
      const turn = {
        ...responseTurn,
        id: `turn-${conversationTurns.length + 1}`,
        request_id: request.request_id,
        sequence: conversationTurns.length + 1,
        question: request.question,
      }
      conversationTurns.push(turn)
      turns.set(conversationTurnsPath[1], conversationTurns)
      return json(turn)
    }
    if (conversationTurnsPath !== null && init?.method === 'DELETE') {
      return new Response(null, { status: 204 })
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
    if (url === '/v1/workspaces/workspace-1/documents/document-1/download') {
      return new Response('original document bytes', {
        status: 200,
        headers: { 'Content-Type': 'application/pdf' },
      })
    }
    if (url === '/v1/agent/ask' && init?.method === 'POST') {
      return answerResponse ?? json({ answer: 'Revenue increased. [1]', sources: [] })
    }
    if (url === '/v1/workspaces/workspace-1/members') return json({ items: [] })
    if (url.startsWith('/v1/admin/users')) return json({ items: [], total: 0, offset: 0, limit: 50 })
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('dashboard application', () => {
  it('opens chat after login and keeps the dashboard available from the drawer', async () => {
    window.history.replaceState(null, '', '/')
    const fetchMock = installApi()
    const user = userEvent.setup()

    render(<App />)

    await user.type(screen.getByLabelText('Username'), 'admin')
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple')
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    expect(await screen.findByRole('region', { name: 'Conversation' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Ask your context' })).not.toBeInTheDocument()
    expect(window.location.pathname).toBe('/ask-context')
    const openNavigation = screen.getByRole('button', { name: 'Open navigation' })
    await user.click(openNavigation)
    expect(openNavigation).toHaveAttribute('aria-expanded', 'true')
    await user.click(screen.getByRole('link', { name: 'Dashboard' }))

    expect(await screen.findByRole('heading', { name: 'Access, ownership, and context boundaries.' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/dashboard')
    expect(screen.getByRole('button', { name: 'New workspace' })).toBeInTheDocument()
    expect(screen.getByText('User directory')).toBeInTheDocument()
    expect(openNavigation).toHaveAttribute('aria-expanded', 'false')
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
        can_download: false,
        can_manage_members: false,
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
    expect(screen.getByText(/Context extraction and indexing happen asynchronously/)).toBeInTheDocument()
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

  it('creates a private conversation and renders saved source references', async () => {
    window.history.replaceState(null, '', '/ask-context')
    writeSessionToken('member-token')
    const turn = json({
      id: 'turn-1',
      request_id: 'request-1',
      sequence: 1,
      question: 'What changed in revenue?',
      answer: `## Revenue summary

Revenue increased in the launch period. [1]

- Growth was strongest in Q3.
- **Margin** remained stable.

| Quarter | Change |
| --- | ---: |
| Q3 | 20% |

Use \`report.csv\` for details.

![external image](https://example.test/image.png)

<script>window.alert('unsafe')</script>`,
      status: 'completed',
      failure_code: null,
      created_at: '2026-09-24T08:00:00Z',
      completed_at: '2026-09-24T08:00:01Z',
      context_available: true,
      sources: [
        {
          workspace_id: workspace.id,
          document_id: 'document-1',
          processing_run_id: 'run-1',
          chunk_id: 'chunk-1',
          ordinal: 4,
          score: 0.91,
          content_hash: 'a'.repeat(64),
          filename: 'launch-review.pdf',
          section_path: ['Results'],
          page_numbers: [3],
        },
        {
          workspace_id: workspace.id,
          document_id: 'document-1',
          processing_run_id: 'run-1',
          chunk_id: 'chunk-2',
          ordinal: 5,
          score: 0.88,
          content_hash: 'b'.repeat(64),
          filename: 'launch-review.pdf',
          section_path: ['Results'],
          page_numbers: [4],
        },
      ],
    })
    const fetchMock = installApi(owner, [workspace], undefined, turn)
    const user = userEvent.setup()

    render(<App />)

    expect(await screen.findByRole('region', { name: 'Conversation' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'New conversation' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'New conversation' }))
    await screen.findByLabelText('Your message')
    await waitFor(() => expect(screen.getByLabelText('Your message')).toBeEnabled())
    await user.type(screen.getByLabelText('Your message'), 'What changed in revenue?')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    expect(await screen.findByRole('heading', { name: 'Answer' })).toBeInTheDocument()
    expect(screen.getByText('Revenue increased in the launch period. [1]')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Revenue summary', level: 2 })).toBeInTheDocument()
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Quarter' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'Q3' })).toBeInTheDocument()
    expect(screen.getByText('report.csv')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'external image' })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Conversation transcript' }).querySelector('script')).toBeNull()
    await user.click(screen.getByText(/Sources \(2\)/))
    expect(screen.getAllByText('launch-review.pdf')).toHaveLength(3)
    expect(screen.getByText(/Page 3/)).toBeInTheDocument()
    expect(screen.getByText('Documents in this conversation (1)')).toBeInTheDocument()
    const conversationPane = screen.getByRole('region', { name: 'Conversation' })
    const transcript = within(conversationPane).getByRole('region', { name: 'Conversation transcript' })
    const conversationTitle = within(conversationPane).getByText('New conversation')
    const documentPanel = screen.getByText('Documents in this conversation (1)').closest('details')
    const composer = screen.getByLabelText('Your message').closest('form')
    expect(transcript).toHaveClass('min-h-0', 'flex-1', 'overflow-y-auto')
    expect(transcript).not.toContainElement(conversationTitle)
    expect(transcript).not.toContainElement(documentPanel)
    expect(transcript).not.toContainElement(composer)
    await user.click(screen.getByText('Documents in this conversation (1)'))
    const createObjectURL = vi.fn((_blob: Blob) => 'blob:document-download')
    const revokeObjectURL = vi.fn()
    const createObjectURLDescriptor = Object.getOwnPropertyDescriptor(window.URL, 'createObjectURL')
    const revokeObjectURLDescriptor = Object.getOwnPropertyDescriptor(window.URL, 'revokeObjectURL')
    Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: createObjectURL })
    Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL })
    const createElement = vi.spyOn(window.document, 'createElement')
    const clickDownload = vi.spyOn(HTMLElement.prototype, 'click').mockImplementation(() => {})
    const downloadButton = screen.getByRole('button', { name: 'Download launch-review.pdf' })
    await user.click(downloadButton)
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) =>
      String(input) === '/v1/workspaces/workspace-1/documents/document-1/download',
    )).toBe(true))
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(clickDownload).toHaveBeenCalledTimes(1))
    const downloadedBlob = createObjectURL.mock.calls[0]?.[0] as Blob | undefined
    expect(downloadedBlob?.size).toBe(23)
    expect(downloadedBlob?.type).toBe('application/pdf')
    const anchorIndex = createElement.mock.calls.findIndex(([tagName]) => tagName === 'a')
    expect(anchorIndex).toBeGreaterThanOrEqual(0)
    const downloadAnchor = createElement.mock.results[anchorIndex]?.value as HTMLAnchorElement
    expect(downloadAnchor.download).toBe('launch-review.pdf')
    const downloadCall = fetchMock.mock.calls.find(([input]) =>
      String(input) === '/v1/workspaces/workspace-1/documents/document-1/download',
    )
    if (downloadCall === undefined) throw new Error('Expected a document download request')
    const downloadHeaders = downloadCall[1]?.headers
    if (!(downloadHeaders instanceof Headers)) throw new Error('Expected download request headers')
    expect(downloadHeaders.get('Authorization')).toBe('Bearer member-token')
    await waitFor(
      () => expect(revokeObjectURL).toHaveBeenCalledWith('blob:document-download'),
      { timeout: 2000 },
    )

    await user.type(screen.getByLabelText('Your message'), 'Give me a follow-up')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Download launch-review.pdf' })).toHaveLength(1))
    expect(screen.getByText('Documents in this conversation (1)')).toBeInTheDocument()
    if (createObjectURLDescriptor === undefined) Reflect.deleteProperty(window.URL, 'createObjectURL')
    else Object.defineProperty(window.URL, 'createObjectURL', createObjectURLDescriptor)
    if (revokeObjectURLDescriptor === undefined) Reflect.deleteProperty(window.URL, 'revokeObjectURL')
    else Object.defineProperty(window.URL, 'revokeObjectURL', revokeObjectURLDescriptor)
    createElement.mockRestore()
    clickDownload.mockRestore()
    const askCall = fetchMock.mock.calls.find(([input]) =>
      String(input) === '/v1/agent/conversations/conversation-1/turns'
      && (input instanceof Request ? input.method : '') !== 'GET',
    )
    if (askCall === undefined) throw new Error('Expected a conversation turn request')
    const headers = askCall[1]?.headers
    if (!(headers instanceof Headers)) throw new Error('Expected request headers')
    expect(headers.get('Authorization')).toBe('Bearer member-token')
    expect(headers.get('Content-Type')).toBe('application/json')
    const body = JSON.parse(String(askCall[1]?.body)) as { request_id: string; question: string }
    expect(body.question).toBe('What changed in revenue?')
    expect(body.request_id).toBeTruthy()
  })

  it('keeps source citations visible but hides downloads from workspace viewers', async () => {
    window.history.replaceState(null, '', '/ask-context')
    writeSessionToken('viewer-token')
    const viewerWorkspace: Workspace = {
      ...workspace,
      membership_role: 'viewer',
      can_upload: false,
      can_download: false,
      can_manage_members: false,
    }
    const turn = json({
      answer: 'The review covers launch results. [1]',
      status: 'completed',
      failure_code: null,
      created_at: '2026-09-24T08:00:00Z',
      completed_at: '2026-09-24T08:00:01Z',
      context_available: true,
      sources: [{
        workspace_id: viewerWorkspace.id,
        document_id: 'document-1',
        processing_run_id: 'run-1',
        chunk_id: 'chunk-1',
        ordinal: 4,
        score: 0.91,
        content_hash: 'a'.repeat(64),
        filename: 'launch-review.pdf',
        section_path: ['Results'],
        page_numbers: [3],
      }],
    })
    installApi(owner, [viewerWorkspace], undefined, turn)
    const user = userEvent.setup()

    render(<App />)

    await screen.findByRole('region', { name: 'Conversation' })
    await user.click(await screen.findByRole('button', { name: 'New conversation' }))
    await user.type(await screen.findByLabelText('Your message'), 'Summarize the launch review')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    expect(await screen.findByRole('heading', { name: 'Answer' })).toBeInTheDocument()
    await user.click(screen.getByText(/Sources \(1\)/))

    expect(screen.getByText('launch-review.pdf')).toBeInTheDocument()
    expect(screen.queryByText(/Documents in this conversation/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Download launch-review.pdf/ })).not.toBeInTheDocument()
  })

  it('shows the active process and recent workspace activity in notifications', async () => {
    window.history.replaceState(null, '', '/ask-context')
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
    installApi(owner, [workspace], undefined, undefined, {
      active: { item: activeRun },
      recent: {
        items: [activeRun, completedRun],
        total: 2,
        since: '2026-09-24T07:00:00Z',
      },
    })
    const user = userEvent.setup()

    render(<App />)

    await screen.findByRole('region', { name: 'Conversation' })
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
      window.history.replaceState(null, '', '/ask-context')
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
    window.history.replaceState(null, '', '/ask-context')
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
    installApi(owner, [workspace], undefined, undefined, processingResponses)
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
    window.history.replaceState(null, '', '/ask-context')
    writeSessionToken('member-token')
    installApi(owner)
    const user = userEvent.setup()

    render(<App />)

    await screen.findByRole('region', { name: 'Conversation' })
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
})
