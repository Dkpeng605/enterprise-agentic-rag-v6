import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { workspaceApi, type Collection, type DocumentSummary } from '../src/api/workspace'
import DocumentsView from '../src/views/DocumentsView.vue'

vi.mock('../src/api/workspace', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/workspace')>()
  return {
    ...original,
    workspaceApi: {
      listCollections: vi.fn(), createCollection: vi.fn(), updateCollection: vi.fn(),
      deleteCollection: vi.fn(), listDocuments: vi.fn(), getDocument: vi.fn(),
      uploadDocument: vi.fn(), deleteDocument: vi.fn(), listJobs: vi.fn(), getJob: vi.fn(),
    },
  }
})

const seed: Collection = {
  id: '01900000-0000-7000-8000-000000000101', name: 'Demo Knowledge', description: null,
  visibility: 'tenant', is_seed: true, document_count: 1, ready_document_count: 1,
  created_at: '2026-09-13T01:00:00Z', updated_at: '2026-09-13T01:00:00Z',
}
const team: Collection = { ...seed, id: '01900000-0000-7000-8000-000000000102', name: '团队制度', is_seed: false }
const document: DocumentSummary = {
  id: '01900000-0000-7000-8000-000000000201', collection_id: team.id, title: '员工手册',
  organization: '示例公司', status: 'ready', visibility: 'tenant',
  version_id: '01900000-0000-7000-8000-000000000202', source_name: 'handbook.pdf',
  media_type: 'application/pdf', size_bytes: 4096, sha256_prefix: '123456789abc',
  created_at: '2026-09-13T01:00:00Z', updated_at: '2026-09-13T01:02:00Z',
}

function mountDocuments() {
  return mount(DocumentsView, { global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
}

describe('documents and collection management', () => {
  beforeEach(() => {
    for (const method of Object.values(workspaceApi)) vi.mocked(method).mockReset()
    vi.mocked(workspaceApi.listCollections).mockResolvedValue([seed, team])
    vi.mocked(workspaceApi.listDocuments).mockResolvedValue({ items: [document] })
    vi.mocked(workspaceApi.createCollection).mockResolvedValue(team)
    vi.mocked(workspaceApi.updateCollection).mockResolvedValue(team)
    vi.mocked(workspaceApi.deleteCollection).mockResolvedValue()
    vi.mocked(workspaceApi.deleteDocument).mockResolvedValue('01900000-0000-7000-8000-000000000401')
    vi.mocked(workspaceApi.uploadDocument).mockResolvedValue({
      document_id: document.id, version_id: document.version_id,
      job_id: '01900000-0000-7000-8000-000000000301', deduplicated: false, status: 'pending',
    })
    vi.mocked(workspaceApi.getDocument).mockResolvedValue({
      ...document, root_count: 4, leaf_count: 12, version_error_code: null,
      version_error_message: null, recent_job: {
        id: '01900000-0000-7000-8000-000000000301', document_id: document.id,
        version_id: document.version_id, type: 'ingest', status: 'succeeded', attempts: 1,
        max_attempts: 3, available_at: '2026-09-13T01:00:00Z', heartbeat_at: null,
        progress: 100, stage: 'persist', error_code: null, error_message: null, cancel_requested: false,
      },
    })
  })

  it('filters tenant documents and opens persisted detail data', async () => {
    const wrapper = mountDocuments()
    await flushPromises()
    await wrapper.get('input[type="search"]').setValue('员工')
    await wrapper.get('input[type="search"]').trigger('keyup.enter')
    await flushPromises()
    expect(workspaceApi.listDocuments).toHaveBeenLastCalledWith(expect.objectContaining({ keyword: '员工' }))

    await wrapper.get('.document-identity').trigger('click')
    await flushPromises()
    expect(workspaceApi.getDocument).toHaveBeenCalledWith(document.id)
    expect(wrapper.get('.detail-facts').text()).toContain('4 / 12')
    expect(wrapper.text()).toContain('打开任务详情')
  })

  it('creates, edits, and confirmation-deletes a non-seed collection', async () => {
    const wrapper = mountDocuments()
    await flushPromises()
    await wrapper.get('.heading-actions .button--secondary').trigger('click')
    await wrapper.get('.workspace-sheet input').setValue('研发资料')
    await wrapper.get('.workspace-sheet form').trigger('submit')
    await flushPromises()
    expect(workspaceApi.createCollection).toHaveBeenCalledWith(expect.objectContaining({ name: '研发资料' }))

    await wrapper.get(`[aria-label="编辑集合 ${team.name}"]`).trigger('click')
    await wrapper.get('.workspace-sheet input').setValue('团队规范')
    await wrapper.get('.workspace-sheet form').trigger('submit')
    await flushPromises()
    expect(workspaceApi.updateCollection).toHaveBeenCalledWith(team.id, expect.objectContaining({ name: '团队规范' }))

    await wrapper.get(`[aria-label="删除集合 ${team.name}"]`).trigger('click')
    await wrapper.get('.workspace-sheet input').setValue(team.name)
    await wrapper.get('.danger-button').trigger('click')
    await flushPromises()
    expect(workspaceApi.deleteCollection).toHaveBeenCalledWith(team.id, team.name)
    expect(wrapper.text()).toContain('后台任务安全清理')
    expect(wrapper.get(`[aria-label="删除集合 ${seed.name}"]`).attributes()).toHaveProperty('disabled')
  })

  it('uploads a real File and links its server-issued ingestion job', async () => {
    const wrapper = mountDocuments()
    await flushPromises()
    await wrapper.findAll('.collection-row > button')[1]!.trigger('click')
    await wrapper.get('.heading-actions .button--primary').trigger('click')
    const fileInput = wrapper.get('input[type="file"]')
    const file = new File(['policy'], 'policy.txt', { type: 'text/plain' })
    Object.defineProperty(fileInput.element, 'files', { value: [file] })
    await fileInput.trigger('change')
    await wrapper.get('.workspace-sheet form').trigger('submit')
    await flushPromises()

    expect(workspaceApi.uploadDocument).toHaveBeenCalledWith(expect.objectContaining({
      file, collectionId: team.id, title: 'policy', visibility: 'tenant',
    }))
    expect(wrapper.get('.upload-result').text()).toContain('查看处理进度')
  })

  it('creates an idempotent delete job from the document list', async () => {
    const wrapper = mountDocuments()
    await flushPromises()
    await wrapper.get(`[aria-label="删除文档 ${document.title}"]`).trigger('click')
    await wrapper.get('.danger-button').trigger('click')
    await flushPromises()
    expect(workspaceApi.deleteDocument).toHaveBeenCalledWith(document.id)
    expect(wrapper.text()).toContain('删除任务已创建')
  })
})
