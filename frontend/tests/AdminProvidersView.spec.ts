import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { providerApi, type ProviderCatalog } from '../src/api/providers'
import AdminProvidersView from '../src/views/AdminProvidersView.vue'

vi.mock('../src/api/providers', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/providers')>()
  return { ...original, providerApi: { load: vi.fn(), select: vi.fn(), indexStatus: vi.fn(), reindex: vi.fn() } }
})

const catalog: ProviderCatalog = {
  providers: [{
    kind: 'embedding', name: 'local_multilingual_minilm', version: 'local-v1',
    capabilities: ['documents'], is_remote: false, health: 'healthy',
  }],
  options: [
    {
      kind: 'embedding', key: 'local', name: 'local_multilingual_minilm', model: 'local',
      label: '本地模型', provider: 'FastEmbed', capabilities: ['documents'], is_remote: false,
      dimension: 384, input_token_limit: 128, language_note: 'multilingual', note: null,
      selected: true, available: true, unavailable_reason: null, requires_restart: true,
    },
    {
      kind: 'embedding', key: 'BAAI/bge-m3', name: 'siliconflow', model: 'BAAI/bge-m3',
      label: 'SiliconFlow BGE-M3', provider: 'SiliconFlow', capabilities: ['remote'], is_remote: true,
      dimension: 1024, input_token_limit: 8192, language_note: '100+ languages',
      note: '切换后必须重新摄取', selected: false, available: false,
      unavailable_reason: '需要在 backend 环境配置 SILICONFLOW_API_KEY', requires_restart: true,
    },
    {
      kind: 'reranker', key: 'BAAI/bge-reranker-v2-m3', name: 'siliconflow',
      model: 'BAAI/bge-reranker-v2-m3', label: 'SiliconFlow BGE Reranker v2 M3',
      provider: 'SiliconFlow', capabilities: ['remote'], is_remote: true, dimension: null,
      input_token_limit: 8192, language_note: 'multilingual', note: '官方 API /rerank',
      selected: false, available: true, unavailable_reason: null, requires_restart: true,
    },
  ],
  selection: {
    embedding_model: 'local', reranker_model: 'local-reranker', llm_model: 'minimax-m3',
    pending_restart: false,
  },
}

const indexStatus = {
  active_revision: 'mac-semantic-current', embedding_model: 'local', embedding_dimension: 384,
  total_documents: 1, compatible_documents: 0, incompatible_documents: 1,
  documents: [{ document_id: 'doc-1', title: '演示文档', version_id: 'version-1',
    stored_revisions: ['mac-semantic-old'], active_revision: 'mac-semantic-current', compatible: false,
    root_count: 2, leaf_count: 3, vector_count: 0 }],
}

describe('Provider management', () => {
  beforeEach(() => {
    vi.mocked(providerApi.load).mockReset().mockResolvedValue(catalog)
    vi.mocked(providerApi.select).mockReset().mockResolvedValue({
      ...catalog, selection: { ...catalog.selection, pending_restart: true },
    })
    vi.mocked(providerApi.indexStatus).mockReset().mockResolvedValue(indexStatus)
    vi.mocked(providerApi.reindex).mockReset().mockResolvedValue({
      active_revision: indexStatus.active_revision, requested_count: 1, rebuilt_count: 1,
      skipped_count: 0, failed_count: 0, cleanup_failed_count: 0, items: [],
    })
  })

  it('shows SiliconFlow model contracts and disables profiles without credentials', async () => {
    const wrapper = mount(AdminProvidersView)
    await flushPromises()

    expect(wrapper.text()).toContain('BAAI/bge-m3')
    expect(wrapper.text()).toContain('1024 维')
    expect(wrapper.text()).toContain('8192 tokens')
    expect(wrapper.text()).toContain('未配置')
    const unavailable = wrapper.find('input[value="BAAI/bge-m3"]')
    expect(unavailable.attributes('disabled')).toBeDefined()
  })

  it('persists an available SiliconFlow reranker selection', async () => {
    const wrapper = mount(AdminProvidersView)
    await flushPromises()

    await wrapper.find('input[value="BAAI/bge-reranker-v2-m3"]').trigger('change')
    await flushPromises()

    expect(providerApi.select).toHaveBeenCalledWith('reranker', 'BAAI/bge-reranker-v2-m3')
    expect(wrapper.text()).toContain('重启 Mac backend 后生效')
  })

  it('shows incompatible documents and offers a real revision rebuild', async () => {
    const wrapper = mount(AdminProvidersView)
    await flushPromises()

    expect(wrapper.text()).toContain('需要重建')
    expect(wrapper.text()).toContain('mac-semantic-old')
    const rebuildButton = wrapper.findAll('button').find((button) => button.text().includes('重建'))
    expect(rebuildButton).toBeDefined()
    await rebuildButton!.trigger('click')
    await flushPromises()

    expect(providerApi.reindex).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('重建完成')
  })
})
