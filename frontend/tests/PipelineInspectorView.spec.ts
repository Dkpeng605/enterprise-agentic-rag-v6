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
  cleaner_provider: 'deterministic', cleaner_version: '1',
  splitter_provider: 'structure_aware', splitter_version: '1',
  splitter_settings: {
    target_tokens: 350, max_tokens: 480, overlap_tokens: 50,
    tokenizer: 'deterministic-multilingual-v1',
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
      start_offset: 0, end_offset: 5, token_count: 4, overlap_chars: 0, metadata: {},
    },
    {
      id: 'leaf_02', ordinal: 1, text: '请在七日内提交。', retrieval_text: '请在七日内提交。',
      start_offset: 3, end_offset: 13, token_count: 9, overlap_chars: 2, metadata: {},
    },
  ],
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
    expect(wrapper.get('.cleaning-audit').text()).toContain('whitespace')
    expect(wrapper.get('.text-compare').text()).toContain('退款   申请')
    expect(wrapper.findAll('.leaf-card')).toHaveLength(2)
    expect(wrapper.findAll('.leaf-card')[1]!.text()).toContain('overlap 2 chars')
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
})
