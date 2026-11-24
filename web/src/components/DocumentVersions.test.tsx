import { useState } from 'react'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { ApiResource } from '../hooks/useApiResource'
import type { WorkspaceDocumentsData } from '../hooks/useWorkspaceDocuments'
import type {
  DocumentSearchHit,
  DocumentUpload,
  DocumentVersionsDetail,
  DocumentVersionSuggestion,
  Workspace,
} from '../types'
import { DocumentSearch } from './DocumentSearch'
import { DocumentViewerDrawer } from './DocumentViewerDrawer'
import { WorkspaceDocumentsPage } from './WorkspaceDocumentsPage'

vi.mock('../lib/pdf', () => ({
  loadPdf: vi.fn(async () => ({
    pdf: { numPages: 1 },
    destroy: vi.fn(async () => undefined),
  })),
  renderPdfPage: vi.fn(() => ({
    promise: Promise.resolve({ width: 600, height: 800 }),
    cancel: vi.fn(),
  })),
  renderPdfTextLayer: vi.fn(() => ({
    promise: Promise.resolve(),
    cancel: vi.fn(),
  })),
}))

const workspace: Workspace = {
  id: 'ws-1',
  slug: 'finance',
  name: 'Finance',
  description: 'Financial reports and policies.',
  created_at: '2026-01-01T00:00:00Z',
  membership_role: 'owner',
  member_count: 2,
  can_upload: true,
  can_download: true,
  can_search_context: true,
  can_manage_members: true,
  can_delete: true,
}

const docV1: DocumentUpload = {
  id: 'doc-v1-1111',
  workspace_id: 'ws-1',
  original_filename: 'SOP_Cuti_2024.pdf',
  detected_media_type: 'application/pdf',
  size_bytes: 20480,
  sha256: 'a'.repeat(64),
  status: 'ready',
  created_by_user_id: 'user-1',
  created_at: '2025-01-10T00:00:00Z',
  duplicate: false,
  row_count: 0,
  profile: {
    status: 'enriched',
    title: 'Kebijakan Cuti Karyawan 2024',
    doc_category: 'policy_sop',
    doc_subtype: 'HR Leave Policy',
    primary_entities: [{ name: 'PT Nusantara', type: 'company', aliases: [] }],
    counterparties: [],
    effective_date: '2024-01-01',
    period_label: 'FY2024',
    document_number: 'SOP-HR-01',
    version_label: 'v1',
    language: 'id',
    summary: 'Aturan cuti tahunan 2024.',
    topics: ['leave', 'hr'],
    model: 'gpt-4o-mini',
    updated_at: '2025-01-10T00:05:00Z',
  },
  version: {
    group_id: 'grp-1',
    group_name: 'PT Nusantara — HR Leave Policy',
    relation_kind: 'supersedes',
    version_rank: 1,
    version_count: 2,
    is_latest: false,
    is_superseded: true,
    latest_document_id: 'doc-v2-2222',
    latest_filename: 'SOP_Cuti_2026.pdf',
    latest_period_label: 'FY2026',
    version_note: null,
  },
  pending_version_suggestions: 0,
}

const docV2: DocumentUpload = {
  id: 'doc-v2-2222',
  workspace_id: 'ws-1',
  original_filename: 'SOP_Cuti_2026.pdf',
  detected_media_type: 'application/pdf',
  size_bytes: 24576,
  sha256: 'b'.repeat(64),
  status: 'ready',
  created_by_user_id: 'user-1',
  created_at: '2026-01-10T00:00:00Z',
  duplicate: false,
  row_count: 0,
  profile: {
    status: 'enriched',
    title: 'Kebijakan Cuti Karyawan 2026',
    doc_category: 'policy_sop',
    doc_subtype: 'HR Leave Policy',
    primary_entities: [{ name: 'PT Nusantara', type: 'company', aliases: [] }],
    counterparties: [],
    effective_date: '2026-01-01',
    period_label: 'FY2026',
    document_number: 'SOP-HR-01',
    version_label: 'v2',
    language: 'id',
    summary: 'Pembaruan aturan cuti tahunan 2026.',
    topics: ['leave', 'hr'],
    model: 'gpt-4o-mini',
    updated_at: '2026-01-10T00:05:00Z',
  },
  version: {
    group_id: 'grp-1',
    group_name: 'PT Nusantara — HR Leave Policy',
    relation_kind: 'supersedes',
    version_rank: 2,
    version_count: 2,
    is_latest: true,
    is_superseded: false,
    latest_document_id: 'doc-v2-2222',
    latest_filename: 'SOP_Cuti_2026.pdf',
    latest_period_label: 'FY2026',
    version_note: 'Updated leave quota',
  },
  pending_version_suggestions: 1,
}

const docCandidate: DocumentUpload = {
  id: 'doc-v3-3333',
  workspace_id: 'ws-1',
  original_filename: 'SOP_Cuti_2026_Rev2.pdf',
  detected_media_type: 'application/pdf',
  size_bytes: 25600,
  sha256: 'c'.repeat(64),
  status: 'ready',
  created_by_user_id: 'user-1',
  created_at: '2026-06-15T00:00:00Z',
  duplicate: false,
  row_count: 0,
  profile: {
    status: 'basic',
    title: 'SOP Cuti 2026 Rev 2',
    doc_category: 'policy_sop',
    doc_subtype: 'HR Leave Policy',
    primary_entities: [{ name: 'PT Nusantara', type: 'company', aliases: [] }],
    counterparties: [],
    effective_date: '2026-06-01',
    period_label: 'H2 2026',
    document_number: 'SOP-HR-01',
    version_label: 'Rev2',
    language: 'id',
    summary: null,
    topics: [],
    model: null,
    updated_at: '2026-06-15T00:01:00Z',
  },
  version: null,
  pending_version_suggestions: 1,
}

const pendingSuggestion: DocumentVersionSuggestion = {
  id: 'sug-1',
  workspace_id: 'ws-1',
  status: 'pending',
  confidence: 0.91,
  suggested_relation: 'supersedes',
  suggested_newer_document_id: 'doc-v3-3333',
  reasons: ['Same document number (SOP-HR-01)', 'Same primary entity (PT Nusantara)'],
  document_a: {
    document_id: 'doc-v2-2222',
    filename: 'SOP_Cuti_2026.pdf',
    title: 'Kebijakan Cuti Karyawan 2026',
    doc_category: 'policy_sop',
    doc_subtype: 'HR Leave Policy',
    period_label: 'FY2026',
    effective_date: '2026-01-01',
    version_label: 'v2',
    primary_entities: ['PT Nusantara'],
    created_at: '2026-01-10T00:00:00Z',
  },
  document_b: {
    document_id: 'doc-v3-3333',
    filename: 'SOP_Cuti_2026_Rev2.pdf',
    title: 'SOP Cuti 2026 Rev 2',
    doc_category: 'policy_sop',
    doc_subtype: 'HR Leave Policy',
    period_label: 'H2 2026',
    effective_date: '2026-06-01',
    version_label: 'Rev2',
    primary_entities: ['PT Nusantara'],
    created_at: '2026-06-15T00:00:00Z',
  },
  created_at: '2026-06-15T00:02:00Z',
  resolved_at: null,
}

function makeResource(items: DocumentUpload[]): ApiResource<WorkspaceDocumentsData> {
  return {
    data: { items, total: items.length, offset: 0, limit: 50, complete: true },
    loading: false,
    error: null,
    reload: vi.fn(),
    setData: vi.fn(),
  }
}

describe('document intelligence and versioning UI', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('shows metadata subtitles, version badges, hide-superseded filter, and pending suggestion confirmation', async () => {
    const user = userEvent.setup()
    const documents = makeResource([docV1, docV2, docCandidate])
    const onNotice = vi.fn()

    vi.spyOn(api, 'listVersionSuggestions')
      .mockResolvedValueOnce({
        items: [pendingSuggestion],
        total: 1,
      })
      .mockResolvedValue({
        items: [],
        total: 0,
      })
    const confirmSpy = vi.spyOn(api, 'confirmVersionSuggestion').mockResolvedValue({
      group_id: 'grp-1',
      name: 'PT Nusantara — HR Leave Policy',
      relation_kind: 'supersedes',
      members: [],
    })

    render(
      <WorkspaceDocumentsPage
        workspace={workspace}
        documents={documents}
        canView
        onView={vi.fn()}
        onUpload={vi.fn()}
        onDeleteDocument={vi.fn()}
        onNotice={onNotice}
      />,
    )

    // Metadata subtitle + version badges render in the table
    const table = screen.getByRole('table')
    expect(within(table).getByText('PT Nusantara · HR Leave Policy · FY2024')).toBeInTheDocument()
    expect(within(table).getByRole('button', { name: 'Superseded · v1/2' })).toBeInTheDocument()
    expect(within(table).getByRole('button', { name: 'Latest · v2/2' })).toBeInTheDocument()

    // Pending suggestions banner loads and can be expanded & confirmed
    const banner = await screen.findByRole('region', { name: 'Pending version suggestions' })
    expect(within(banner).getByText('1 potential version match detected')).toBeInTheDocument()
    await user.click(within(banner).getByRole('button', { name: 'Review' }))
    expect(within(banner).getByText(/Same document number \(SOP-HR-01\)/)).toBeInTheDocument()

    await user.click(within(banner).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => {
      expect(confirmSpy).toHaveBeenCalledWith('ws-1', 'sug-1', {
        newer_document_id: 'doc-v3-3333',
        relation_kind: 'supersedes',
      })
    })
    expect(onNotice).toHaveBeenCalledWith('Version link confirmed.')

    // Hide superseded filter hides v1 and restores when clicked again
    const hideBtn = screen.getByRole('button', { name: 'Hide superseded (1)' })
    await user.click(hideBtn)
    expect(within(table).queryByText('SOP_Cuti_2024.pdf')).not.toBeInTheDocument()
    expect(within(table).getByText('SOP_Cuti_2026.pdf')).toBeInTheDocument()
    await user.click(hideBtn)
    expect(within(table).getByText('SOP_Cuti_2024.pdf')).toBeInTheDocument()

    // Search input matches extracted entity/period metadata, not just filename
    const searchInput = screen.getByLabelText('Filter documents by name, entity, type, or period')
    await user.type(searchInput, 'FY2024')
    expect(within(table).getByText('SOP_Cuti_2024.pdf')).toBeInTheDocument()
    expect(within(table).queryByText('SOP_Cuti_2026.pdf')).not.toBeInTheDocument()
  })

  it('opens DocumentVersionsDialog and supports inspecting history and switching relation mode', async () => {
    const user = userEvent.setup()
    const documents = makeResource([docV1, docV2, docCandidate])
    const onNotice = vi.fn()

    vi.spyOn(api, 'listVersionSuggestions').mockResolvedValue({ items: [], total: 0 })
    const detail: DocumentVersionsDetail = {
      document_id: docV2.id,
      workspace_id: workspace.id,
      profile: docV2.profile ?? null,
      group: {
        group_id: 'grp-1',
        name: 'PT Nusantara — HR Leave Policy',
        relation_kind: 'supersedes',
        members: [
          {
            document_id: docV1.id,
            filename: docV1.original_filename,
            title: 'Kebijakan Cuti Karyawan 2024',
            version_label: 'v1',
            version_rank: 1,
            is_latest: false,
            version_note: null,
            period_label: 'FY2024',
            effective_date: '2024-01-01',
            linked_at: '2026-01-10T00:05:00Z',
            created_at: docV1.created_at,
          },
          {
            document_id: docV2.id,
            filename: docV2.original_filename,
            title: 'Kebijakan Cuti Karyawan 2026',
            version_label: 'v2',
            version_rank: 2,
            is_latest: true,
            version_note: 'Updated leave quota',
            period_label: 'FY2026',
            effective_date: '2026-01-01',
            linked_at: '2026-01-10T00:05:00Z',
            created_at: docV2.created_at,
          },
        ],
      },
      pending_suggestions: [],
    }
    vi.spyOn(api, 'getDocumentVersions').mockResolvedValue(detail)
    const updateSpy = vi.spyOn(api, 'updateVersionGroup').mockResolvedValue({
      ...detail.group!,
      relation_kind: 'periodic_series',
    })

    render(
      <WorkspaceDocumentsPage
        workspace={workspace}
        documents={documents}
        canView
        onView={vi.fn()}
        onUpload={vi.fn()}
        onDeleteDocument={vi.fn()}
        onNotice={onNotice}
      />,
    )

    await user.click(
      screen.getByRole('button', {
        name: `Intelligence and versions for ${docV2.original_filename}`,
      }),
    )

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Kebijakan Cuti Karyawan 2026')).toBeInTheDocument()
    expect(within(dialog).getByText('PT Nusantara — HR Leave Policy')).toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Periodic series' }))
    await waitFor(() => {
      expect(updateSpy).toHaveBeenCalledWith('ws-1', 'grp-1', {
        relation_kind: 'periodic_series',
      })
    })
  })

  it('shows a superseded banner in DocumentViewerDrawer with an Open latest action', async () => {
    const user = userEvent.setup()
    const onOpenDocument = vi.fn()
    vi.spyOn(api, 'fetchDocumentBlob').mockResolvedValue(
      new Blob(['%PDF-1.7 fake'], { type: 'application/pdf' }),
    )

    render(
      <DocumentViewerDrawer
        document={docV1}
        workspace={workspace}
        onUnauthorized={vi.fn()}
        onClose={vi.fn()}
        onOpenDocument={onOpenDocument}
      />,
    )

    const note = screen.getByRole('note')
    expect(within(note).getByText(/Superseded revision/)).toBeInTheDocument()
    expect(within(note).getByText('SOP_Cuti_2026.pdf')).toBeInTheDocument()

    await user.click(within(note).getByRole('button', { name: 'Open latest' }))
    expect(onOpenDocument).toHaveBeenCalledWith('doc-v2-2222')
  })

  it('displays metadata subtitle and version badge on DocumentSearch hits', async () => {
    const user = userEvent.setup()
    const hit: DocumentSearchHit = {
      document_id: docV1.id,
      workspace_id: workspace.id,
      workspace_name: workspace.name,
      filename: docV1.original_filename,
      media_type: 'application/pdf',
      size_bytes: 20480,
      status: 'ready',
      row_count: 0,
      created_at: docV1.created_at,
      title: 'Kebijakan Cuti Karyawan 2024',
      doc_category: 'policy_sop',
      doc_subtype: 'HR Leave Policy',
      primary_entities: ['PT Nusantara'],
      period_label: 'FY2024',
      version: docV1.version,
    }
    vi.spyOn(api, 'searchDocuments').mockResolvedValue({ items: [hit], total: 1 })

    function ControlledSearch() {
      const [query, setQuery] = useState('')
      return (
        <DocumentSearch
          value={query}
          onChange={setQuery}
          onOpenDocument={vi.fn()}
          onUnauthorized={vi.fn()}
        />
      )
    }

    render(<ControlledSearch />)

    const input = screen.getByLabelText('Search documents')
    await user.type(input, 'Nusantara')

    expect(await screen.findByText('PT Nusantara · HR Leave Policy · FY2024')).toBeInTheDocument()
    expect(screen.getByText('Superseded · v1/2')).toBeInTheDocument()
  })
})
