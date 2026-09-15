import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  workspaceApi,
  type DocumentPipeline,
  type PipelineRoot,
} from '../src/api/workspace'
import PipelineInspectorView from '../src/views/PipelineInspectorView.vue'

vi.mock('../src/api/workspace', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/workspace')>()
  return {
    ...original,
    workspaceApi: {
      getDocumentPipeline: vi.fn(),
      getPipelineRoot: vi.fn(),
      getDocumentImageUrl: vi.fn((documentId: string, sha256: string) => `/api/v1/documents/${documentId}/images/${sha256}`),
      getLlmCleaningPreflight: vi.fn(),
      runLlmCleaning: vi.fn(),
    },
  }
})

const rootSummary = {
  id: 'root_01',
  ordinal: 0,
  kind: 'paragraph',
  source_locator: { page: 1, heading: '退款政策' },
  raw_chars: 34,
  clean_chars: 32,
  changed: true,
  leaf_count: 2,
  cleaning_audit: [{
    rule: 'whitespace', occurrences: 2,
    before_sha256: 'aaaaaaaaaaaaaaaa', after_sha256: 'bbbbbbbbbbbbbbbb',
  }],
}

const pipeline: DocumentPipeline = {
  document_id: '01900000-0000-7000-8000-000000000201',
  version_id: '01900000-0000-7000-8000-000000000202',
  source_name: 'refund-policy.md',
  parser_provider: 'text', parser_version: '1',
  cleaner_provider: 'deterministic', cleaner_version: '2',
  splitter_provider: 'structure_aware', splitter_version: '2',
  splitter_settings: {
    target_tokens: 350, max_tokens: 480, overlap_tokens: 50,
    embedding_token_limit: 512, hard_cut_count: 0,
    tokenizer: 'fastembed-tokenizer:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
  },
  llm_cleaning: {},
  root_count: 1, leaf_count: 2, roots: [rootSummary], next_cursor: null,
}

const preflight = {
  document_id: pipeline.document_id,
  version_id: pipeline.version_id,
  available: true,
  reason: null,
  provider: 'openai_compatible',
  model: 'minimax-m3',
  remote: true,
  root_count: 1,
  input_chars: 32,
  max_roots: 20,
  max_input_chars: 12000,
  estimated_calls: 1,
  max_output_tokens: 8000,
  already_applied: false,
}

const detail: PipelineRoot = {
  summary: rootSummary,
  raw_text: '退款   申请\n\n\n请在七日内提交。',
  clean_text: '退款 申请\n\n请在七日内提交。',
  metadata: {},
  leaves: [
    {
      id: 'leaf_01', ordinal: 0, text: '退款 申请', retrieval_text: '退款 申请',
      start_offset: 0, end_offset: 5, token_count: 4, overlap_chars: 0,
      metadata: { boundary: 'sentence', hard_cut: false },
    },
    {
      id: 'leaf_02', ordinal: 1, text: '请在七日内提交。', retrieval_text: '请在七日内提交。',
      start_offset: 3, end_offset: 13, token_count: 9, overlap_chars: 2,
      metadata: { boundary: 'sentence', hard_cut: false },
    },
  ],
}

const imageDetail: PipelineRoot = {
  ...detail,
  metadata: {
    images: [{
      page: 2,
      ordinal: 0,
      name: 'architecture.png',
      media_type: 'image/png',
      width: 1024,
      height: 640,
      sha256: 'c'.repeat(64),
      object_key: 'sha256/cc/dd/architecture-object',
      caption: '系统由 API、检索服务和向量库组成。',
      caption_status: 'created',
      caption_error_code: null,
    }, {
      page: 3,
      ordinal: 1,
      name: 'scan.png',
      media_type: 'image/png',
      width: 800,
      height: 600,
      sha256: 'd'.repeat(64),
      object_key: 'sha256/dd/ee/scan-object',
      caption: null,
      caption_status: 'degraded',
      caption_error_code: 'VISION_CAPTION_FAILED',
    }],
    image_captions: ['系统由 API、检索服务和向量库组成。'],
    vision_degraded: true,
    vision_provider: 'openai_compatible',
    vision_model: 'minimax-m3',
    vision_remote: true,
    vision_image_count: 2,
    vision_caption_count: 1,
    vision_caption_status_counts: { created: 1, skipped: 0, degraded: 1 },
  },
  leaves: detail.leaves.map((leaf, index) => index === 0
    ? { ...leaf, retrieval_text: `${leaf.text}\n\n系统由 API、检索服务和向量库组成。` }
    : leaf),
}

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace/documents/:documentId/pipeline', component: PipelineInspectorView },
      { path: '/workspace/documents', component: { template: '<div />' } },
    ],
  })
  await router.push(`/workspace/documents/${pipeline.document_id}/pipeline`)
  await router.isReady()
  return mount(PipelineInspectorView, { global: { plugins: [router] } })
}

describe('document pipeline inspector', () => {
  beforeEach(() => {
    vi.mocked(workspaceApi.getDocumentPipeline).mockReset().mockResolvedValue(pipeline)
    vi.mocked(workspaceApi.getPipelineRoot).mockReset().mockResolvedValue(detail)
    vi.mocked(workspaceApi.getLlmCleaningPreflight).mockReset().mockResolvedValue(preflight)
    vi.mocked(workspaceApi.runLlmCleaning).mockReset().mockResolvedValue({
      document_id: pipeline.document_id,
      version_id: pipeline.version_id,
      provider: 'openai_compatible',
      model: 'minimax-m3',
      root_count: 1,
      changed_root_count: 1,
      leaf_count_before: 2,
      leaf_count_after: 3,
      input_chars: 32,
      output_chars: 30,
      input_tokens: 20,
      output_tokens: 12,
      retry_count: 0,
      llm_calls: 1,
      applied_at: '2026-09-14T08:00:00Z',
    })
  })

  it('shows the persisted parser, cleaner, splitter and actual chunk boundaries', async () => {
    const wrapper = await mountView()
    await flushPromises()

    expect(workspaceApi.getDocumentPipeline).toHaveBeenCalledWith(pipeline.document_id)
    expect(workspaceApi.getPipelineRoot).toHaveBeenCalledWith(pipeline.document_id, 'root_01')
    expect(wrapper.get('.pipeline-flow').text()).toContain('deterministic')
    expect(wrapper.get('.pipeline-config').text()).toContain('350')
    expect(wrapper.get('.pipeline-config').text()).toContain('512')
    expect(wrapper.get('.cleaning-audit').text()).toContain('whitespace')
    expect(wrapper.get('.text-compare').text()).toContain('退款   申请')
    expect(wrapper.findAll('.leaf-card')).toHaveLength(2)
    expect(wrapper.findAll('.leaf-card')[1]!.text()).toContain('overlap 2 chars')
    expect(wrapper.findAll('.leaf-card')[1]!.text()).toContain('sentence')
    expect(wrapper.get('.llm-cleaning-panel').text()).toContain('minimax-m3')
    expect(wrapper.get('.llm-cleaning-panel').text()).toContain('32 chars')
  })

  it('requires an explicit remote-data confirmation before one cleaning call', async () => {
    const wrapper = await mountView()
    await flushPromises()

    await wrapper.get('.llm-cleaning-actions button').trigger('click')
    const confirm = wrapper.get('.llm-confirm')
    expect(confirm.text()).toContain('clean_text 将离开本机')
    expect(confirm.get('.button--primary').attributes('disabled')).toBeDefined()

    await confirm.get('input[type="checkbox"]').setValue(true)
    await confirm.get('.button--primary').trigger('click')
    await flushPromises()

    expect(workspaceApi.runLlmCleaning).toHaveBeenCalledTimes(1)
    expect(workspaceApi.runLlmCleaning).toHaveBeenCalledWith(
      pipeline.document_id,
      pipeline.version_id,
    )
    expect(wrapper.get('.llm-cleaning-result').text()).toContain('1/1 Roots 变化')
    expect(wrapper.get('.llm-cleaning-result').text()).toContain('Tokens 20 in / 12 out')
  })

  it('shows persisted Root-level LLM hashes even when the model returns unchanged text', async () => {
    vi.mocked(workspaceApi.getPipelineRoot).mockResolvedValue({
      ...detail,
      metadata: {
        llm_cleaning: {
          provider: 'openai_compatible', model: 'minimax-m3', changed: false,
          before_sha256: '1234567890abcdefaaaa', after_sha256: '1234567890abcdefaaaa',
          applied_at: '2026-09-14T08:00:00Z', input_tokens: 20, output_tokens: 12,
        },
      },
    })
    const wrapper = await mountView()
    await flushPromises()

    expect(wrapper.get('.root-llm-audit').text()).toContain('模型保守原样返回')
    expect(wrapper.get('.root-llm-audit').text()).toContain('1234567890abcdef → 1234567890abcdef')
    expect(wrapper.get('.root-llm-audit').text()).toContain('20 in / 12 out')
  })

  it('shows persisted image facts, caption status, provider and retrieval inclusion', async () => {
    vi.mocked(workspaceApi.getPipelineRoot).mockResolvedValue(imageDetail)
    const wrapper = await mountView()
    await flushPromises()

    const panel = wrapper.get('.image-enrichment-panel')
    expect(panel.text()).toContain('2 张图片')
    expect(panel.text()).toContain('openai_compatible / minimax-m3')
    expect(panel.text()).toContain('远程 Vision · 已记录')
    expect(panel.text()).toContain('architecture.png')
    expect(panel.text()).toContain('第 2 页 · 1024 × 640 · image/png')
    expect(panel.text()).toContain('c'.repeat(12))
    expect(panel.text()).toContain('系统由 API、检索服务和向量库组成。')
    expect(panel.text()).toContain('Caption 已进入首个 Leaf 的 retrieval_text')
    expect(panel.text()).toContain('scan.png')
    expect(panel.text()).toContain('degraded · 已降级')
    expect(panel.text()).toContain('VISION_CAPTION_FAILED')
    expect(panel.text()).toContain('created 1 · skipped 0 · degraded 1')
    expect(panel.findAll('img')).toHaveLength(2)
    expect(panel.find('img')?.attributes('src')).toContain(`/images/${'c'.repeat(64)}`)
  })

  it('states when a persisted Root contains no extracted images', async () => {
    vi.mocked(workspaceApi.getPipelineRoot).mockResolvedValue({
      ...detail,
      metadata: {
        images: [],
        vision_image_count: 0,
        vision_caption_count: 0,
        vision_caption_status_counts: { created: 0, skipped: 0, degraded: 0 },
        vision_provider: 'none',
        vision_model: '1',
        vision_remote: false,
      },
    })
    const wrapper = await mountView()
    await flushPromises()

    expect(wrapper.get('.image-enrichment-panel').text()).toContain('当前 Root 没有 Loader 提取的图片')
  })
})
