import createClient, { type Middleware } from 'openapi-fetch'

import type { components, paths } from './schema'

export type SessionProfile = components['schemas']['AuthMeResponse']
export type LoginInput = components['schemas']['LoginRequest']

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly requestId?: string,
    readonly retryAfterSeconds?: number,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

type ErrorPayload = {
  error?: {
    code?: string
    message?: string
    request_id?: string
    details?: Record<string, unknown>
  }
}

let csrfToken: string | undefined
let requestIdListener: ((requestId: string) => void) | undefined
let unauthorizedListener: (() => void) | undefined

export function configureApiSession(options: {
  csrfToken?: string
  onRequestId?: (requestId: string) => void
  onUnauthorized?: () => void
}): void {
  csrfToken = options.csrfToken
  requestIdListener = options.onRequestId
  unauthorizedListener = options.onUnauthorized
}

const sessionMiddleware: Middleware = {
  onRequest({ request }) {
    if (csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(request.method.toUpperCase())) {
      request.headers.set('X-CSRF-Token', csrfToken)
    }
    return request
  },
  onResponse({ response, schemaPath }) {
    const requestId = response.headers.get('X-Request-ID')
    if (requestId) requestIdListener?.(requestId)
    if (response.status === 401 && schemaPath !== '/api/v1/auth/login') {
      unauthorizedListener?.()
    }
  },
  onError({ error }) {
    return new ApiError(
      error instanceof Error ? error.message : '无法连接服务，请检查网络后重试。',
      0,
      'NETWORK_ERROR',
    )
  },
}

export const apiClient = createClient<paths>({
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? globalThis.location?.origin ?? '',
  credentials: 'include',
})
apiClient.use(sessionMiddleware)

export function apiErrorFromResponse(error: unknown, response: Response): ApiError {
  const payload = (error ?? {}) as ErrorPayload
  const detail = payload.error
  const retryAfter = Number(response.headers.get('Retry-After'))
  return new ApiError(
    detail?.message ?? `请求失败（HTTP ${response.status}）`,
    response.status,
    detail?.code ?? 'HTTP_ERROR',
    detail?.request_id ?? response.headers.get('X-Request-ID') ?? undefined,
    Number.isFinite(retryAfter) ? retryAfter : undefined,
    detail?.details ?? {},
  )
}

export interface AuthApi {
  me(): Promise<SessionProfile>
  login(input: LoginInput): Promise<SessionProfile>
  logout(): Promise<void>
}

export const authApi: AuthApi = {
  async me() {
    const result = await apiClient.GET('/api/v1/auth/me')
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async login(input) {
    const result = await apiClient.POST('/api/v1/auth/login', { body: input })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async logout() {
    const result = await apiClient.POST('/api/v1/auth/logout')
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
  },
}
