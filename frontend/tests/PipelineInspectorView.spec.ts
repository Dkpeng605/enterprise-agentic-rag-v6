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
  root_count: 1, leaf_count: 2, roots: [rootSummary], next_cursor: null,
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
  })
})
