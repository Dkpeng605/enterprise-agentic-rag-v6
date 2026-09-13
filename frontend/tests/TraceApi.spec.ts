// @vitest-environment node

import { afterEach, describe, expect, it, vi } from 'vitest'

describe('query trace generated API adapter', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it('encodes server-side mode, status, degraded, and cursor filters', async () => {
    vi.stubGlobal('location', { origin: 'https://example.test' })
    let capturedRequest!: Request
    vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
      capturedRequest = request
      return Response.json({ items: [], next_cursor: null })
    }))
    const { traceApi } = await import('../src/api/traces')

    await traceApi.listQueries({
      mode: 'deep', status: 'abstained', degraded: true, cursor: 'opaque-cursor', limit: 12,
    })

    expect(capturedRequest.url).toContain('/api/v1/traces/query?')
    expect(capturedRequest.url).toContain('mode=deep')
    expect(capturedRequest.url).toContain('status=abstained')
    expect(capturedRequest.url).toContain('degraded=true')
    expect(capturedRequest.url).toContain('cursor=opaque-cursor')
    expect(capturedRequest.url).toContain('limit=12')
  })
})
