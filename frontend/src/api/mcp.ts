import type { components } from './schema'

import { apiClient, apiErrorFromResponse } from './client'

export type McpCapabilityCatalog = components['schemas']['McpCapabilityCatalogResponse']
export type McpTransport = components['schemas']['McpTransportResponse']

export interface McpApi {
  load(): Promise<McpCapabilityCatalog>
}

export const mcpApi: McpApi = {
  async load() {
    const result = await apiClient.GET('/api/v1/workspace/mcp')
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
}
