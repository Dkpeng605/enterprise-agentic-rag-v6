import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../src/api/client'
import { traceApi, type IngestionTraceView, type TraceSummary } from '../src/api/traces'
import IngestionTraceViewComponent from '../src/views/IngestionTraceView.vue'

vi.mock('../src/api/traces', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/traces')>()
  return { ...original, traceApi: { listIngestion: vi.fn(), getIngestion: vi.fn() } }
})

const summary: TraceSummary = {
  trace_id: '44444444444444444444444444444444', trace_type: 'ingestion',
  subject_id: '01900000-0000-7000-8000-000000000801', mode: null, status: 'succeeded',
  started_at: '2026-09-13T03:00:00Z', finished_at: '2026-09-13T03:00:00.500Z',
  duration_ms: 500, span_count: 5, degraded: false,
}

const successDetail: IngestionTraceView = {
  summary, attempt: 1, progress: 100, completed: true, error_code: null,
  stages: [
    { span_id: '0000000000000001', parent_span_id: null, name: 'rag.ingestion', offset_ms: 0, duration_ms: 500, status: 'UNSET', root_count: null, leaf_count: null, expected_count: null, verified_count: null, batch_count: null },
    { span_id: '0000000000000002', parent_span_id: '0000000000000001', name: 'rag.ingestion.load', offset_ms: 10, duration_ms: 60, status: 'UNSET', root_count: 2, leaf_count: null, expected_count: null, verified_count: null, batch_count: null },
    { span_id: '0000000000000003', parent_span_id: '0000000000000001', name: 'rag.ingestion.project', offset_ms: 150, duration_ms: 200, status: 'UNSET', root_count: null, leaf_count: null, expected_count: 5, verified_count: 5, batch_count: 3 },
  ],
  batches: [{ span_id: '0000000000000004', parent_span_id: '0000000000000003', phase: 'staging', batch_index: 1, batch_count: 3, item_count: 2, written_count: 2, offset_ms: 170, duration_ms: 20, status: 'UNSET' }],
}

function mountView() {
  return mount(IngestionTraceViewComponent, {
    global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } },
  })
}

describe('ingestion trace workspace', () => {
  beforeEach(() => {
    vi.mocked(traceApi.listIngestion).mockReset().mockResolvedValue({ items: [summary] })
    vi.mocked(traceApi.getIngestion).mockReset().mockResolvedValue(successDetail)
  })

  it('renders successful pipeline stages, counts, and projection batches', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-testid="ingestion-waterfall"]').text()).toContain('2 Root')
    expect(wrapper.get('[data-testid="ingestion-waterfall"]').text()).toContain('5/5 verified')
    expect(wrapper.get('[data-testid="ingestion-batches"]').text()).toContain('STAGING')
    expect(wrapper.get('[data-testid="ingestion-batches"]').text()).toContain('1 / 3')
  })

  it('shows the failed stage and only the persisted stable error code', async () => {
    vi.mocked(traceApi.listIngestion).mockResolvedValue({ items: [{ ...summary, trace_id: '55555555555555555555555555555555', status: 'failed' }] })
    vi.mocked(traceApi.getIngestion).mockResolvedValue({
      ...successDetail,
      summary: { ...summary, trace_id: '55555555555555555555555555555555', status: 'failed' },
      progress: 40, completed: false, error_code: 'INTERNAL_ERROR', batches: [],
      stages: [{ ...successDetail.stages[1]!, status: 'ERROR', name: 'rag.ingestion.split' }],
    })
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('.ingestion-trace-error').text()).toContain('INTERNAL_ERROR')
    expect(wrapper.get('.waterfall-bar--degraded').classes()).toContain('waterfall-bar--degraded')
    expect(wrapper.text()).not.toContain('vendor details')
    expect(wrapper.text()).toContain('未进入向量批处理')
  })

  it('keeps loading, empty, and request-id error states explicit', async () => {
    let resolve!: (value: { items: TraceSummary[] }) => void
    vi.mocked(traceApi.listIngestion).mockReturnValue(new Promise((done) => { resolve = done }))
    const loadingWrapper = mountView()
    expect(loadingWrapper.get('[aria-busy="true"]').attributes('aria-label')).toContain('正在载入')
    resolve({ items: [] })
    await flushPromises()
    expect(loadingWrapper.get('[data-testid="ingestion-trace-empty"]').text()).toContain('Worker 完成一次')

    vi.mocked(traceApi.listIngestion).mockRejectedValue(new ApiError('trace unavailable', 503, 'SERVICE_UNAVAILABLE', 'request-m7-06'))
    const errorWrapper = mountView()
    await flushPromises()
    expect(errorWrapper.get('[role="alert"]').text()).toContain('request-m7-06')
  })
})
