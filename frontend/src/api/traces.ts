import type { components } from './schema'

import { apiClient, apiErrorFromResponse } from './client'

export type TraceSummary = components['schemas']['TraceSummaryResponse']
export type QueryTraceView = components['schemas']['QueryTraceViewResponse']
export type IngestionTraceView = components['schemas']['IngestionTraceViewResponse']
export type QueryMode = 'standard' | 'deep'
export type QueryTraceStatus = 'answered' | 'partial' | 'abstained' | 'no_results' | 'error' | 'cancelled'
export type IngestionTraceStatus = 'succeeded' | 'failed' | 'retry_wait' | 'cancelled'
export type TraceFilters = {
  mode?: QueryMode
  status?: QueryTraceStatus
  degraded?: boolean
  cursor?: string
  limit?: number
}
export type TracePage = { items: TraceSummary[]; nextCursor?: string }
export type IngestionTraceFilters = {
  status?: IngestionTraceStatus
  cursor?: string
  limit?: number
}

export interface TraceApi {
  listQueries(filters?: TraceFilters): Promise<TracePage>
  getQuery(traceId: string): Promise<QueryTraceView>
  listIngestion(filters?: IngestionTraceFilters): Promise<TracePage>
  getIngestion(traceId: string): Promise<IngestionTraceView>
}

export const traceApi: TraceApi = {
  async listQueries(filters = {}) {
    const result = await apiClient.GET('/api/v1/traces/query', {
      params: {
        query: {
          mode: filters.mode,
          status: filters.status,
          degraded: filters.degraded,
          cursor: filters.cursor,
          limit: filters.limit ?? 20,
        },
      },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return { items: result.data.items, nextCursor: result.data.next_cursor ?? undefined }
  },
  async getQuery(traceId) {
    const result = await apiClient.GET('/api/v1/traces/query/{trace_id}', {
      params: { path: { trace_id: traceId } },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async listIngestion(filters = {}) {
    const result = await apiClient.GET('/api/v1/traces/ingestion', {
      params: {
        query: {
          status: filters.status,
          cursor: filters.cursor,
          limit: filters.limit ?? 20,
        },
      },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return { items: result.data.items, nextCursor: result.data.next_cursor ?? undefined }
  },
  async getIngestion(traceId) {
    const result = await apiClient.GET('/api/v1/traces/ingestion/{trace_id}', {
      params: { path: { trace_id: traceId } },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
}
