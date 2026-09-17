import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { overviewApi, type OverviewSnapshot } from '../src/api/overview'
import ModelStatusView from '../src/views/ModelStatusView.vue'

vi.mock('../src/api/overview', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/overview')>()
  return { ...original, overviewApi: { load: vi.fn() } }
})

const snapshot: OverviewSnapshot = {
  overview: {
    generated_at: '2026-09-17T01:02:00Z', collection_count: 1,
    document_counts: { pending: 0, processing: 0, ready: 2, failed: 0, deleting: 0 },
    root_count: 4, leaf_count: 12, queries_24h: 3, query_errors_24h: 0,
    query_error_rate: 0, query_p95_ms: 420, recent_activity: [],
    query_outcome_counts: {}, query_abstention_rate: 0, query_answer_rate: 1,
    query_generation_degraded_24h: 0,
  },
  health: {
    status: 'healthy', ready: true, checks: [],
    providers: [
      { kind: 'llm', name: 'openai_compatible', version: 'MiniMax-M3', capabilities: ['chat'], is_remote: true, health: 'healthy' },
      { kind: 'embedding', name: 'siliconflow', version: 'BAAI/bge-m3', capabilities: ['dense'], is_remote: true, health: 'healthy' },
      { kind: 'vector_store', name: 'milvus', version: '2', capabilities: ['dense'], is_remote: false, health: 'healthy' },
    ],
  },
}

describe('model status workspace', () => {
  beforeEach(() => vi.mocked(overviewApi.load).mockReset().mockResolvedValue(snapshot))

  it('shows model health while keeping shared runtime selection behind admin access', async () => {
    const wrapper = mount(ModelStatusView, {
      global: {
        plugins: [createPinia()],
        stubs: { RouterLink: { template: '<a><slot /></a>' } },
      },
    })
    await flushPromises()

    expect(wrapper.get('h1').text()).toBe('模型状态与选配')
    expect(wrapper.findAll('.model-status-card')).toHaveLength(2)
    expect(wrapper.text()).toContain('MiniMax-M3')
    expect(wrapper.text()).toContain('BAAI/bge-m3')
    expect(wrapper.get('.model-selection-boundary').text()).toContain('管理员登录后选配')
  })
})
