import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../src/api/client'
import { mcpApi, type McpCapabilityCatalog } from '../src/api/mcp'
import McpView from '../src/views/McpView.vue'

vi.mock('../src/api/mcp', () => ({
  mcpApi: { load: vi.fn() },
}))

const catalog: McpCapabilityCatalog = {
  server_name: 'enterprise-agentic-rag-v6',
  server_version: '0.1.0',
  tools: [
    { name: 'query_knowledge_base', description: '回答', required_scopes: ['mcp:access', 'query:execute'], read_only: true },
    { name: 'search_documents', description: '搜索', required_scopes: ['mcp:access', 'knowledge:read'], read_only: true },
  ],
  resources: [
    { uri: 'rag://collections', kind: 'resource', description: '集合', required_scopes: ['knowledge:read'] },
    { uri: 'rag://documents/{document_id}', kind: 'template', description: '文档', required_scopes: ['knowledge:read'] },
  ],
  transports: [
    { name: 'stdio', status: 'requires_factory', endpoint: null, detail: '需要组合函数' },
    { name: 'streamable_http', status: 'external_composition_required', endpoint: null, detail: '需要外部组合' },
  ],
}

function mountView() {
  return mount(McpView)
}

describe('MCP capability catalog view', () => {
  beforeEach(() => vi.mocked(mcpApi.load).mockReset())

  it('renders tools, resource templates, scopes, and honest transport states from the API', async () => {
    vi.mocked(mcpApi.load).mockResolvedValue(catalog)
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[aria-labelledby="mcp-tools-title"]').text()).toContain('query_knowledge_base')
    expect(wrapper.text()).toContain('rag://documents/{document_id}')
    expect(wrapper.text()).toContain('mcp:access')
    expect(wrapper.text()).toContain('需要外部组合')
    expect(wrapper.text()).not.toContain('api_key')
  })

  it('shows a request id and supports retry after an API failure', async () => {
    vi.mocked(mcpApi.load)
      .mockRejectedValueOnce(new ApiError('服务不可用', 503, 'SERVICE_UNAVAILABLE', 'mcp-request-1'))
      .mockResolvedValueOnce(catalog)
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('mcp-request-1')
    await wrapper.get('[role="alert"] button').trigger('click')
    await flushPromises()
    expect(vi.mocked(mcpApi.load)).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('query_knowledge_base')
  })
})
