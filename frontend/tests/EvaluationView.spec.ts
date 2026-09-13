import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../src/api/client'
import {
  evaluationApi,
  type EvaluationCatalog,
  type EvaluationRun,
} from '../src/api/evaluations'
import EvaluationView from '../src/views/EvaluationView.vue'

vi.mock('../src/api/evaluations', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/evaluations')>()
  return {
    ...original,
    evaluationApi: {
      catalog: vi.fn(), create: vi.fn(), list: vi.fn(), get: vi.fn(), compare: vi.fn(),
    },
  }
})

const catalog: EvaluationCatalog = {
  dataset_revision: 'enterprise-demo-golden-2026-09-09',
  dataset_label: 'Golden Set v1 · local deterministic smoke',
  case_counts: { all: 30, standard: 21, deep: 9 },
  profiles: [{
    id: 'deterministic-sparse-v1', label: 'Deterministic sparse smoke',
    provider: 'hashing_lexical', model: 'hash-v1', prompt_revision: 'none',
    requires_remote: false, estimated_llm_calls_per_case: 0,
  }],
  max_cases: 30,
  max_llm_calls: 0,
}

const run: EvaluationRun = {
  id: '01900000-0000-7000-8000-000000000701', status: 'succeeded',
  dataset_revision: catalog.dataset_revision, mode: 'all',
  provider_profile: 'deterministic-sparse-v1', provider: 'hashing_lexical', model: 'hash-v1',
  prompt_revision: 'none', index_revision: catalog.dataset_revision, commit_sha: 'abc123456789',
  max_cases: 10, max_llm_calls: 0, estimated_llm_calls: 0,
  completed_cases: 10, total_cases: 10, case_ids: ['case-1'],
  aggregate_metrics: { document_recall_at_5: 1, mrr_at_10: 0.96 },
  usage: { llm_calls: 0, embedding_calls: 0, rerank_calls: 0, tokens: 0 },
  error_code: null, started_at: '2026-09-13T07:00:00Z', finished_at: '2026-09-13T07:00:01Z',
  created_at: '2026-09-13T07:00:00Z',
  report: { cases: [{ case_id: 'case-low', metrics: { mrr_at_10: 0.5 } }] },
}

function mountView() {
  return mount(EvaluationView)
}

describe('evaluation workspace', () => {
  beforeEach(() => {
    vi.mocked(evaluationApi.catalog).mockReset().mockResolvedValue(catalog)
    vi.mocked(evaluationApi.list).mockReset().mockResolvedValue({ items: [run] })
    vi.mocked(evaluationApi.get).mockReset().mockResolvedValue(run)
    vi.mocked(evaluationApi.create).mockReset().mockResolvedValue({ ...run, status: 'queued', completed_cases: 0, report: null })
    vi.mocked(evaluationApi.compare).mockReset()
  })

  it('shows the preflight budget, persisted metrics, snapshots, and failed cases', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-testid="evaluation-launcher"]').text()).toContain('10 Cases · ≤ 0 LLM calls')
    expect(wrapper.get('[data-testid="evaluation-metrics"]').text()).toContain('96.0%')
    expect(wrapper.text()).toContain('hashing_lexical / hash-v1')
    expect(wrapper.text()).toContain('case-low')
  })

  it('starts a bounded run with the selected generated contract', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('.evaluation-launcher select').setValue('deep')
    await wrapper.get('.evaluation-launcher input').setValue(7)
    await wrapper.get('.evaluation-launcher .button').trigger('click')
    await flushPromises()

    expect(evaluationApi.create).toHaveBeenCalledWith({
      dataset_revision: catalog.dataset_revision,
      mode: 'deep', provider_profile: 'deterministic-sparse-v1', max_cases: 7, max_llm_calls: 0,
    })
  })

  it('refreshes the selected detail once more when history reaches a terminal state', async () => {
    const queued = { ...run, status: 'queued' as const, completed_cases: 0, report: null }
    vi.mocked(evaluationApi.list)
      .mockResolvedValueOnce({ items: [queued] })
      .mockResolvedValueOnce({ items: [run] })
    vi.mocked(evaluationApi.get)
      .mockResolvedValueOnce(queued)
      .mockResolvedValueOnce(run)
    const wrapper = mountView()
    await flushPromises()

    await vi.waitFor(() => expect(evaluationApi.get).toHaveBeenCalledTimes(2))
    await flushPromises()
    expect(wrapper.get('[data-testid="evaluation-metrics"]').text()).toContain('96.0%')
  })

  it('renders stable reasons instead of deltas for incomparable runs', async () => {
    const second = { ...run, id: '01900000-0000-7000-8000-000000000702' }
    vi.mocked(evaluationApi.list).mockResolvedValue({ items: [run, second] })
    vi.mocked(evaluationApi.compare).mockResolvedValue({
      base_run_id: run.id, candidate_run_id: second.id, comparable: false,
      reasons: ['INDEX_MISMATCH', 'CASE_SET_MISMATCH'], base_metrics: {}, candidate_metrics: {}, deltas: {},
    })
    const wrapper = mountView()
    await flushPromises()
    const selects = wrapper.findAll('[data-testid="evaluation-compare"] select')
    await selects[0]!.setValue(run.id)
    await selects[1]!.setValue(second.id)
    await wrapper.get('[data-testid="evaluation-compare"] > button').trigger('click')
    await flushPromises()

    expect(wrapper.get('.comparison-result--blocked').text()).toContain('Index revision 不一致')
    expect(wrapper.get('.comparison-result--blocked').text()).toContain('实际 Case 集合不一致')
  })

  it('keeps loading, empty, and request-id error states explicit', async () => {
    vi.mocked(evaluationApi.list).mockResolvedValue({ items: [] })
    const empty = mountView()
    expect(empty.find('[aria-busy="true"]').exists()).toBe(true)
    await flushPromises()
    expect(empty.get('[data-testid="evaluation-empty"]').text()).toContain('还没有匹配')

    vi.mocked(evaluationApi.catalog).mockRejectedValue(new ApiError('eval unavailable', 503, 'SERVICE_UNAVAILABLE', 'request-m7-07'))
    const failed = mountView()
    await flushPromises()
    expect(failed.get('[role="alert"]').text()).toContain('request-m7-07')
  })
})
