import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../src/api/client'
import { traceApi, type QueryTraceView, type TraceSummary } from '../src/api/traces'
import QueryTraceViewComponent from '../src/views/QueryTraceView.vue'

vi.mock('../src/api/traces', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/traces')>()
  return { ...original, traceApi: { listQueries: vi.fn(), getQuery: vi.fn() } }
})

const summary: TraceSummary = {
  trace_id: '11111111111111111111111111111111', trace_type: 'query',
  subject_id: '01900000-0000-7000-8000-000000000701', mode: 'standard', status: 'answered',
  started_at: '2026-09-13T02:00:00Z', finished_at: '2026-09-13T02:00:00.250Z',
  duration_ms: 250, span_count: 4, degraded: false,
}

const standardDetail: QueryTraceView = {
  summary,
  usage: { llm_calls: 2, input_tokens: 120 },
  stages: [
    { span_id: '0000000000000001', parent_span_id: null, name: 'rag.dense_retrieval', offset_ms: 20, duration_ms: 35, status: 'UNSET', degraded: false },
    { span_id: '0000000000000002', parent_span_id: null, name: 'rag.rrf_fusion', offset_ms: 70, duration_ms: 8, status: 'UNSET', degraded: false },
  ],
  rankings: [{
    leaf_id: 'leaf_01', root_id: 'root_01', dense_rank: 1, sparse_rank: 4, rrf_rank: 2,
    rerank_rank: 1, dense_score: 0.91, sparse_score: 0.72, rrf_score: 0.032,
    rerank_score: 0.97,
  }],
  recovery_rounds: [], degradations: [],
  plan: {
    provider: 'deterministic', degraded: false,
    original_query: '如何部署；同时如何回滚', rewritten_query: '如何部署；同时如何回滚',
    intent: 'procedural', language: 'zh', sub_queries: ['如何部署', '如何回滚'],
  },
  retrieval_branches: [{
    branch_index: 0, query: '如何部署', dense_requested: 40, dense_returned: 8,
    sparse_requested: 40, sparse_returned: 6, sparse_algorithm: 'milvus_builtin_bm25', overlap_count: 3, unique_count: 11,
  }],
  stage_metrics: [{
    stage: 'rrf_fusion', input_count: 14, output_count: 10, dropped_count: 4,
    attributes: { ranked_lists: 4, unique_leaves: 10 },
  }],
}

function mountView() {
  return mount(QueryTraceViewComponent, {
    global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } },
  })
}

describe('query trace workspace', () => {
  beforeEach(() => {
    vi.mocked(traceApi.listQueries).mockReset().mockResolvedValue({ items: [summary] })
    vi.mocked(traceApi.getQuery).mockReset().mockResolvedValue(standardDetail)
  })

  it('renders the real waterfall and Dense/Sparse to RRF/Rerank movement', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-testid="waterfall"]').text()).toContain('Dense Retrieval')
    expect(wrapper.get('[data-testid="rank-table"]').text()).toContain('leaf_01')
    expect(wrapper.get('[data-testid="rank-table"]').text()).toContain('#4')
    expect(wrapper.get('[data-testid="rank-table"]').text()).toContain('0.970')
    expect(wrapper.get('[data-testid="query-plan"]').text()).toContain('共 2 条并行检索分支')
    expect(wrapper.get('[data-testid="retrieval-metrics"]').text()).toContain('8 / 40')
    expect(wrapper.get('[data-testid="retrieval-metrics"]').text()).toContain('BM25（Milvus 原生）')
    expect(wrapper.get('[data-testid="retrieval-metrics"]').text()).toContain('不等同于 Recall@K')
  })

  it('distinguishes Deep recovery rounds and provenance counts', async () => {
    vi.mocked(traceApi.listQueries).mockResolvedValue({ items: [{ ...summary, mode: 'deep', status: 'abstained' }] })
    vi.mocked(traceApi.getQuery).mockResolvedValue({
      ...standardDetail,
      summary: { ...summary, mode: 'deep', status: 'abstained' },
      recovery_rounds: [{
        round_number: 1, route: 'hyde_dense', retrieval_mode: 'dense_only', target_count: 2,
        returned_count: 3, added_count: 2, duplicate_count: 1,
      }],
    })
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-testid="recovery-rounds"]').text()).toContain('HyDE · Dense')
    expect(wrapper.get('[data-testid="recovery-rounds"]').text()).toContain('新增 2')
    expect(wrapper.text()).toContain('已拒答')
  })

  it('labels a degraded component without exposing raw exceptions', async () => {
    vi.mocked(traceApi.listQueries).mockResolvedValue({ items: [{ ...summary, degraded: true }] })
    vi.mocked(traceApi.getQuery).mockResolvedValue({
      ...standardDetail,
      summary: { ...summary, degraded: true },
      degradations: [{ component: 'reranker', provider: 'bge-reranker' }],
    })
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('.trace-degraded').text()).toContain('Reranker 回退')
    expect(wrapper.get('.trace-degraded').text()).toContain('bge-reranker')
    expect(wrapper.text()).not.toContain('stack trace')
  })

  it('keeps loading, empty, and request-id error states explicit', async () => {
    let resolve!: (value: { items: TraceSummary[] }) => void
    vi.mocked(traceApi.listQueries).mockReturnValue(new Promise((done) => { resolve = done }))
    const loadingWrapper = mountView()
    expect(loadingWrapper.get('[aria-busy="true"]').attributes('aria-label')).toContain('正在载入')
    resolve({ items: [] })
    await flushPromises()
    expect(loadingWrapper.get('[data-testid="trace-empty"]').text()).toContain('不会由演示数据填充')

    vi.mocked(traceApi.listQueries).mockRejectedValue(new ApiError('trace unavailable', 503, 'SERVICE_UNAVAILABLE', 'request-m7-05'))
    const errorWrapper = mountView()
    await flushPromises()
    expect(errorWrapper.get('[role="alert"]').text()).toContain('request-m7-05')
  })
})
