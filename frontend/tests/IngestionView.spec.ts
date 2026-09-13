import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { workspaceApi, type JobListItem } from '../src/api/workspace'
import IngestionView from '../src/views/IngestionView.vue'

vi.mock('../src/api/workspace', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/workspace')>()
  return { ...original, workspaceApi: { listJobs: vi.fn(), getJob: vi.fn() } }
})

const queued: JobListItem = {
  id: '01900000-0000-7000-8000-000000000501', document_id: '01900000-0000-7000-8000-000000000502',
  version_id: '01900000-0000-7000-8000-000000000503', type: 'ingest', status: 'queued',
  attempts: 0, max_attempts: 3, available_at: '2026-09-13T01:00:00Z', heartbeat_at: null,
  progress: 0, stage: null, error_code: null, error_message: null, cancel_requested: false,
  created_at: '2026-09-13T01:00:00Z',
}
const failed: JobListItem = {
  ...queued, id: '01900000-0000-7000-8000-000000000504', status: 'failed', progress: 35,
  stage: 'embedding', error_code: 'EMBEDDING_UNAVAILABLE', error_message: 'Embedding provider unavailable.',
}

describe('ingestion task monitoring', () => {
  beforeEach(() => {
    vi.mocked(workspaceApi.listJobs).mockReset().mockResolvedValue({ items: [queued, failed] })
    vi.mocked(workspaceApi.getJob).mockReset()
  })
  afterEach(() => vi.useRealTimers())

  it('shows stable progress and sanitized failure details', async () => {
    const wrapper = mount(IngestionView, { global: { stubs: { RouterLink: true } } })
    await flushPromises()
    expect(wrapper.get('[data-testid="job-list"]').text()).toContain('等待 Worker')
    await wrapper.findAll('[data-testid="job-list"] > button')[1]!.trigger('click')
    expect(wrapper.get('.job-detail').text()).toContain('EMBEDDING_UNAVAILABLE')
    expect(wrapper.get('.job-detail').text()).toContain('35%')
    wrapper.unmount()
  })

  it('polls only while a non-terminal job is present', async () => {
    vi.useFakeTimers()
    const wrapper = mount(IngestionView, { global: { stubs: { RouterLink: true } } })
    await flushPromises()
    expect(workspaceApi.listJobs).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(5_000)
    await flushPromises()
    expect(workspaceApi.listJobs).toHaveBeenCalledTimes(2)

    vi.mocked(workspaceApi.listJobs).mockResolvedValue({ items: [failed] })
    await vi.advanceTimersByTimeAsync(5_000)
    await flushPromises()
    const callsAfterTerminalRefresh = vi.mocked(workspaceApi.listJobs).mock.calls.length
    await vi.advanceTimersByTimeAsync(5_000)
    expect(workspaceApi.listJobs).toHaveBeenCalledTimes(callsAfterTerminalRefresh)
    wrapper.unmount()
  })
})
