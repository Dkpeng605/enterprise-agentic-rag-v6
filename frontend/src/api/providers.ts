import type { components } from './schema'

import { apiClient, apiErrorFromResponse } from './client'

export type ProviderCatalog = components['schemas']['ProviderCatalogResponse']
export type ProviderDiagnostic = components['schemas']['ProviderDiagnosticResponse']
export type ProviderOption = components['schemas']['ProviderOptionResponse']
export type ProviderKind = components['schemas']['ProviderSelectionRequest']['kind']
export type ProviderIndexStatus = components['schemas']['ProviderIndexStatusResponse']
export type ProviderReindexResult = components['schemas']['ProviderReindexResponse']

export interface ProviderApi {
  load(): Promise<ProviderCatalog>
  select(kind: ProviderKind, key: string): Promise<ProviderCatalog>
  indexStatus(): Promise<ProviderIndexStatus>
  reindex(): Promise<ProviderReindexResult>
}

export const providerApi: ProviderApi = {
  async load() {
    const result = await apiClient.GET('/api/v1/admin/providers')
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async select(kind, key) {
    const result = await apiClient.POST('/api/v1/admin/providers/select', {
      body: { kind, key },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async indexStatus() {
    const result = await apiClient.GET('/api/v1/admin/providers/index-status')
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async reindex() {
    const result = await apiClient.POST('/api/v1/admin/providers/reindex')
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
}
