import { afterEach, describe, expect, it, vi } from 'vitest'

describe('generated API client middleware', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it('attaches CSRF only to mutations and captures request IDs', async () => {
    const fetchMock = vi.fn(async (_request: Request) =>
      new Response(null, { status: 204, headers: { 'X-Request-ID': 'request-123' } }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { apiClient, configureApiSession } = await import('../src/api/client')
    const onRequestId = vi.fn()
    configureApiSession({ csrfToken: 'csrf-123', onRequestId })

    await apiClient.POST('/api/v1/auth/logout')

    const request = fetchMock.mock.calls[0]?.[0]
    expect(request).toBeInstanceOf(Request)
    expect(request?.headers.get('X-CSRF-Token')).toBe('csrf-123')
    expect(request?.credentials).toBe('include')
    expect(onRequestId).toHaveBeenCalledWith('request-123')
  })

  it('notifies the auth boundary when a protected request returns 401', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json(
          { error: { code: 'AUTHENTICATION_REQUIRED', message: 'expired', request_id: 'id', details: {} } },
          { status: 401 },
        ),
      ),
    )
    const { apiClient, configureApiSession } = await import('../src/api/client')
    const onUnauthorized = vi.fn()
    configureApiSession({ onUnauthorized })

    await apiClient.GET('/api/v1/system/status')

    expect(onUnauthorized).toHaveBeenCalledOnce()
  })
})
