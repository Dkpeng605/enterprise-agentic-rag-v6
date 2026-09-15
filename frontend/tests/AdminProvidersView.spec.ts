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
  }, {
    kind: 'vision', name: 'none', version: '1', capabilities: ['skip_caption'],
    is_remote: false, health: 'healthy',
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
    {
      kind: 'sparse_encoder', key: 'milvus_builtin_bm25', name: 'milvus_builtin_bm25',
      model: 'jieba-v1', label: 'Milvus 原生 BM25', provider: 'Milvus Lite',
      capabilities: ['bm25', 'corpus_idf', 'jieba'], is_remote: false, dimension: null,
      input_token_limit: null, language_note: '中文 / 中英混合',
      note: '由 Milvus Function 维护 TF/IDF', selected: true, available: true,
      unavailable_reason: null, requires_restart: true,
    },
    {
      kind: 'sparse_encoder', key: 'hashing_lexical', name: 'hashing_lexical',
      model: 'blake2b-31bit-logtf-l2-v1', label: 'Hashing Lexical（离线）', provider: '内置',
      capabilities: ['precomputed'], is_remote: false, dimension: null, input_token_limit: null,
      language_note: null, note: '确定性词法投影', selected: false, available: true,
      unavailable_reason: null, requires_restart: true,
    },
    {
      kind: 'vision', key: 'none', name: 'none', model: 'none', label: '关闭图片 Caption（降级）',
      provider: '内置', capabilities: ['skip_caption'], is_remote: false, dimension: null,
      input_token_limit: null, language_note: null, note: '图片仍会保存，但不调用远程 Vision Provider',
      selected: true, available: true, unavailable_reason: null, requires_restart: true,
    },
    {
      kind: 'vision', key: 'openai_compatible', name: 'openai_compatible', model: '由 VISION_MODEL 环境变量提供',
      label: 'OpenAI-compatible Vision', provider: 'Configured endpoint',
      capabilities: ['image-caption', 'chat-completions'], is_remote: true, dimension: null,
      input_token_limit: null, language_note: '取决于所配置模型', note: '必须配置 Vision endpoint、密钥与模型',
      selected: false, available: true, unavailable_reason: null, requires_restart: true,
    },
  ],
  selection: {
    embedding_model: 'local', reranker_model: 'local-reranker', llm_model: 'minimax-m3',
    vision_provider: 'none', sparse_encoder: 'milvus_builtin_bm25',
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
      ...catalog,
      selection: {
        ...catalog.selection,
        pending_restart: true,
        pending_reranker_model: 'BAAI/bge-reranker-v2-m3',
      },
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
    expect(wrapper.text()).toContain('Milvus 原生 BM25')
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
    expect(wrapper.text()).toContain('待重启生效')
    const input = wrapper.find('input[value="BAAI/bge-reranker-v2-m3"]').element as HTMLInputElement
    expect(input.checked).toBe(true)
  })

  it('persists a Sparse mode selection', async () => {
    const wrapper = mount(AdminProvidersView)
    await flushPromises()

    await wrapper.find('input[value="hashing_lexical"]').trigger('change')
    await flushPromises()

    expect(providerApi.select).toHaveBeenCalledWith('sparse_encoder', 'hashing_lexical')
  })

  it('shows the configured Vision profile and makes it restart-selectable', async () => {
    const wrapper = mount(AdminProvidersView)
    await flushPromises()

    expect(wrapper.text()).toContain('OpenAI-compatible Vision')
    await wrapper.find('input[value="openai_compatible"]').trigger('change')
    await flushPromises()

    expect(providerApi.select).toHaveBeenCalledWith('vision', 'openai_compatible')
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
