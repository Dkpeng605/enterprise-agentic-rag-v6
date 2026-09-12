import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../src/api/client'
import { queryApi, type QueryResult } from '../src/api/query'
import ChatView from '../src/views/ChatView.vue'

vi.mock('../src/api/query', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/query')>()
  return {
    ...original,
    queryApi: { listCollections: vi.fn(), stream: vi.fn() },
  }
})

const answered: QueryResult = {
  query_id: '01900000-0000-7000-8000-000000001001',
  status: 'answered',
  answer: '年假增加两天。[1]',
  citations: [
    {
      id: 1,
      document_id: '01900000-0000-7000-8000-000000001002',
      root_id: 'root-policy-2026',
      chunk_ids: ['leaf-1'],
      source_name: '员工手册.pdf',
      title: '员工手册',
      page: 12,
      section: null,
      quote: '自 2026 年起，年假增加两天。',
      score: 0.93,
    },
  ],
  diagnostics: {},
  usage: { llm_calls: 2 },
  trace_id: '0123456789abcdef0123456789abcdef',
}

describe('public chat browser states', () => {
  beforeEach(() => {
    vi.mocked(queryApi.listCollections).mockReset().mockResolvedValue([])
    vi.mocked(queryApi.stream).mockReset()
  })

  it('renders a successful Deep answer and expandable citations', async () => {
    vi.mocked(queryApi.stream).mockImplementation(async (_input, _signal, emit) => {
      emit({ type: 'accepted', sequence: 1, queryId: answered.query_id })
      emit({ type: 'progress', sequence: 2, stage: 'retrieving' })
      emit({ type: 'completed', sequence: 3, result: answered })
    })
    const wrapper = mount(ChatView)
    await wrapper.findAll('.mode-option')[1]?.trigger('click')
    await wrapper.get('textarea').setValue('年假政策有什么变化？')
    await wrapper.get('[data-testid="chat-composer"]').trigger('submit')
    await flushPromises()

    expect(queryApi.stream).toHaveBeenCalledWith(
      expect.objectContaining({ query: '年假政策有什么变化？', mode: 'deep' }),
      expect.any(AbortSignal),
      expect.any(Function),
    )
    expect(wrapper.text()).toContain('年假增加两天。[1]')
    expect(wrapper.get('[data-testid="citations"]').text()).toContain('员工手册')
    expect(wrapper.text()).toContain('第 12 页')
  })

  it('keeps a bounded abstention visible without inventing citations', async () => {
    vi.mocked(queryApi.stream).mockImplementation(async (_input, _signal, emit) => {
      emit({
        type: 'completed',
        sequence: 1,
        result: { ...answered, status: 'abstained', answer: '现有证据不足，无法回答。', citations: [] },
      })
    })
    const wrapper = mount(ChatView)
    await wrapper.get('textarea').setValue('证据之外的问题')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('现有证据不足，无法回答。')
    expect(wrapper.text()).toContain('已触发有边界拒答')
    expect(wrapper.find('[data-testid="citations"]').exists()).toBe(false)
  })

  it('shows retry-after feedback for an HTTP 429', async () => {
    vi.mocked(queryApi.stream).mockRejectedValue(
      new ApiError('limited', 429, 'RATE_LIMITED', 'request-429', 42),
    )
    const wrapper = mount(ChatView)
    await wrapper.get('textarea').setValue('额度测试')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('约 42 秒后可重试')
  })

  it('aborts once and does not reconnect after the user stops a stream', async () => {
    vi.mocked(queryApi.stream).mockImplementation((_input, signal, emit) => {
      emit({ type: 'accepted', sequence: 1, queryId: answered.query_id })
      return new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('stopped', 'AbortError')))
      })
    })
    const wrapper = mount(ChatView)
    await wrapper.get('textarea').setValue('停止测试')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-testid="stream-status"] button').trigger('click')
    await flushPromises()

    expect(queryApi.stream).toHaveBeenCalledOnce()
    expect(wrapper.get('[role="status"]').text()).toContain('连接已中断')
    expect(wrapper.text()).toContain(answered.query_id)
  })
})
