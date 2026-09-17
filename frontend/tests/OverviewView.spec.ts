import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../src/api/client'
import { overviewApi, type OverviewSnapshot } from '../src/api/overview'
import OverviewView from '../src/views/OverviewView.vue'

vi.mock('../src/api/overview', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/overview')>()
  return { ...original, overviewApi: { load: vi.fn(), seedDemo: vi.fn() } }
})

const snapshot: OverviewSnapshot = {
  overview: {
    generated_at: '2026-09-13T01:02:00Z',
    collection_count: 3,
    document_counts: { pending: 1, processing: 2, ready: 7, failed: 1, deleting: 0 },
    root_count: 21,
    leaf_count: 144,
    queries_24h: 42,
    query_errors_24h: 2,
    query_error_rate: 2 / 42,
    query_outcome_counts: { answered: 35, partial: 2, abstained: 3, no_results: 0, error: 2, failed: 0, cancelled: 0 },
    query_abstention_rate: 3 / 42,
    query_answer_rate: 37 / 42,
    query_generation_degraded_24h: 1,
    query_p95_ms: 780,
    recent_activity: [
      {
        id: '01900000-0000-7000-8000-000000000101',
        kind: 'ingestion',
        status: 'processing',
        label: 'index_document',
        started_at: '2026-09-13T01:01:00Z',
        progress: 60,
      },
    ],
  },
  health: {
    status: 'healthy',
    ready: true,
    checks: [],
    providers: [
      {
        kind: 'embedding',
        name: 'bge-m3-local',
        version: '1.0.0',
        capabilities: ['dense'],
        is_remote: false,
        health: 'healthy',
      },
    ],
  },
}

function mountOverview() {
  return mount(OverviewView, {
    global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } },
  })
}

describe('workspace overview browser states', () => {
  beforeEach(() => {
    vi.mocked(overviewApi.load).mockReset()
    vi.mocked(overviewApi.seedDemo).mockReset()
  })

  it('shows a loading skeleton until both tenant and health data resolve', async () => {
    let resolve!: (value: OverviewSnapshot) => void
    vi.mocked(overviewApi.load).mockReturnValue(new Promise((done) => { resolve = done }))
    const wrapper = mountOverview()

    expect(wrapper.get('[aria-busy="true"]').attributes('aria-label')).toContain('正在载入')
    resolve(snapshot)
    await flushPromises()
    expect(wrapper.find('[aria-busy="true"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('bge-m3-local')
    expect(wrapper.text()).toContain('144')
  })

  it('explains a genuinely empty workspace without synthetic metrics', async () => {
    vi.mocked(overviewApi.load).mockResolvedValue({
      overview: {
        ...snapshot.overview,
        collection_count: 1,
        document_counts: { pending: 0, processing: 0, ready: 0, failed: 0, deleting: 0 },
        root_count: 0,
        leaf_count: 0,
        queries_24h: 0,
        query_errors_24h: 0,
        query_error_rate: null,
        query_p95_ms: null,
        recent_activity: [],
      },
      health: { status: 'healthy', ready: true, checks: [], providers: [] },
    })
    const wrapper = mountOverview()
    await flushPromises()

    expect(wrapper.get('[data-testid="overview-empty"]').text()).toContain('还没有业务数据')
    expect(wrapper.text()).toContain('这里不会用演示数字填充空白')
    expect(wrapper.text()).toContain('未注册')
  })

  it('submits demo documents through the real seed API from an empty workspace', async () => {
    const emptySnapshot: OverviewSnapshot = {
      ...snapshot,
      overview: {
        ...snapshot.overview,
        collection_count: 1,
        document_counts: { pending: 0, processing: 0, ready: 0, failed: 0, deleting: 0 },
        root_count: 0,
        leaf_count: 0,
        queries_24h: 0,
        query_errors_24h: 0,
        query_error_rate: null,
        query_p95_ms: null,
        recent_activity: [],
      },
    }
    vi.mocked(overviewApi.load).mockResolvedValueOnce(emptySnapshot).mockResolvedValueOnce(emptySnapshot)
    vi.mocked(overviewApi.seedDemo).mockResolvedValue({
      collection_id: '01900000-0000-7000-8000-00000000d003',
      documents: [],
    })
    const wrapper = mountOverview()
    await flushPromises()

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(overviewApi.seedDemo).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('已提交 0 份演示文档')
  })

  it('keeps confirmed metrics visible while identifying degraded providers', async () => {
    vi.mocked(overviewApi.load).mockResolvedValue({
      ...snapshot,
      health: {
        status: 'degraded',
        ready: false,
        checks: [{ name: 'postgresql', kind: 'database', required: true, status: 'degraded', latency_ms: 32, code: 'SLOW' }],
        providers: [
          { ...snapshot.health.providers[0]!, health: 'degraded' },
          { kind: 'llm', name: 'qwen', version: '2.5', capabilities: ['chat'], is_remote: true, health: 'unavailable' },
        ],
      },
    })
    const wrapper = mountOverview()
    await flushPromises()

    expect(wrapper.get('.degraded-banner').text()).toContain('部分依赖尚未就绪')
    expect(wrapper.get('.provider-card--unavailable').text()).toContain('qwen')
    expect(wrapper.get('.provider-card--degraded').text()).toContain('bge-m3-local')
    expect(wrapper.text()).toContain('42')
  })

  it('distinguishes unprobed remote providers from a degraded provider', async () => {
    vi.mocked(overviewApi.load).mockResolvedValue({
      ...snapshot,
      health: {
        status: 'degraded',
        ready: true,
        checks: [
          { name: 'llm:openai_compatible', kind: 'provider', required: true, status: 'degraded', latency_ms: 0, code: null },
        ],
        providers: [
          { ...snapshot.health.providers[0]!, health: 'healthy' },
          { kind: 'llm', name: 'openai_compatible', version: 'MiniMax-M3', capabilities: ['chat'], is_remote: true, health: 'unknown' },
        ],
      },
    })
    const wrapper = mountOverview()
    await flushPromises()

    expect(wrapper.text()).toContain('服务运行中 · 远程模型待探测')
    expect(wrapper.get('.degraded-banner').text()).toContain('远程模型尚未完成首次探测')
    expect(wrapper.get('.provider-card--unknown').text()).toContain('待探测')
    expect(wrapper.text()).not.toContain('服务处于降级态')
  })

  it('surfaces the request id and retries after a load error', async () => {
    vi.mocked(overviewApi.load)
      .mockRejectedValueOnce(new ApiError('upstream timeout', 503, 'UPSTREAM_ERROR', 'request-m7-03'))
      .mockResolvedValueOnce(snapshot)
    const wrapper = mountOverview()
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('request-m7-03')
    await wrapper.get('[role="alert"] button').trigger('click')
    await flushPromises()
    expect(overviewApi.load).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('服务运行正常')
  })
})
