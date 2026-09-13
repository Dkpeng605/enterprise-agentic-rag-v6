import type { components } from './schema'

import { apiClient, apiErrorFromResponse } from './client'

export type EvaluationCatalog = components['schemas']['EvaluationCatalogResponse']
export type EvaluationRun = components['schemas']['EvaluationRunResponse']
export type EvaluationRunInput = components['schemas']['EvaluationRunCreate']
export type EvaluationComparison = components['schemas']['EvaluationComparisonResponse']
export type EvaluationStatus = EvaluationRun['status']
export type EvaluationPage = { items: EvaluationRun[]; nextCursor?: string }

export interface EvaluationApi {
  catalog(): Promise<EvaluationCatalog>
  create(input: EvaluationRunInput): Promise<EvaluationRun>
  list(filters?: { status?: EvaluationStatus; cursor?: string; limit?: number }): Promise<EvaluationPage>
  get(runId: string): Promise<EvaluationRun>
  compare(baseRunId: string, candidateRunId: string): Promise<EvaluationComparison>
}

export const evaluationApi: EvaluationApi = {
  async catalog() {
    const result = await apiClient.GET('/api/v1/evaluations/catalog')
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async create(input) {
    const result = await apiClient.POST('/api/v1/evaluations/runs', { body: input })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async list(filters = {}) {
    const result = await apiClient.GET('/api/v1/evaluations/runs', {
      params: { query: { status: filters.status, cursor: filters.cursor, limit: filters.limit ?? 20 } },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return { items: result.data.items, nextCursor: result.data.next_cursor ?? undefined }
  },
  async get(runId) {
    const result = await apiClient.GET('/api/v1/evaluations/runs/{run_id}', {
      params: { path: { run_id: runId } },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async compare(baseRunId, candidateRunId) {
    const result = await apiClient.GET('/api/v1/evaluations/compare', {
      params: { query: { base_run_id: baseRunId, candidate_run_id: candidateRunId } },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
}
