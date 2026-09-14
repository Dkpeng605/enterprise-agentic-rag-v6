import type { components } from './schema'

import { ApiError, apiClient, apiErrorFromResponse } from './client'

export type WorkspaceOverview = components['schemas']['WorkspaceOverviewResponse']
export type HealthReport = components['schemas']['HealthReportResponse']
export type ProviderDiagnostic = components['schemas']['ProviderDiagnosticResponse']
export type DemoSeedResult = components['schemas']['DemoSeedResponse']

export type OverviewSnapshot = {
  overview: WorkspaceOverview
  health: HealthReport
}

export interface OverviewApi {
  load(): Promise<OverviewSnapshot>
  seedDemo(): Promise<DemoSeedResult>
}

export const overviewApi: OverviewApi = {
  async load() {
    const [overview, health] = await Promise.all([
      apiClient.GET('/api/v1/workspace/overview'),
      apiClient.GET('/health/doctor'),
    ])
    if (overview.error) throw apiErrorFromResponse(overview.error, overview.response)
    if (!health.data) {
      throw new ApiError('健康诊断返回了空响应。', health.response.status, 'EMPTY_HEALTH_REPORT')
    }
    return { overview: overview.data, health: health.data }
  },
  async seedDemo() {
    const result = await apiClient.POST('/api/v1/demo/seed')
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
}
