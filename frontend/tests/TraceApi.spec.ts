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

  it('uses the dedicated ingestion list and sanitized detail endpoints', async () => {
    vi.stubGlobal('location', { origin: 'https://example.test' })
    const requests: Request[] = []
    vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
      requests.push(request)
      if (request.url.includes('55555555555555555555555555555555')) {
        return Response.json({
          summary: { trace_id: '55555555555555555555555555555555', trace_type: 'ingestion', subject_id: '01900000-0000-7000-8000-000000000001', mode: null, status: 'failed', started_at: '2026-09-13T00:00:00Z', finished_at: '2026-09-13T00:00:01Z', duration_ms: 1000, span_count: 0, degraded: false },
          attempt: 1, progress: 40, completed: false, error_code: 'INTERNAL_ERROR', stages: [], batches: [],
        })
      }
      return Response.json({ items: [], next_cursor: null })
    }))
    const { traceApi } = await import('../src/api/traces')

    await traceApi.listIngestion({ status: 'failed', cursor: 'ingestion-cursor', limit: 8 })
    await traceApi.getIngestion('55555555555555555555555555555555')

    expect(requests[0]?.url).toContain('/api/v1/traces/ingestion?status=failed')
    expect(requests[0]?.url).toContain('cursor=ingestion-cursor')
    expect(requests[1]?.url).toContain('/api/v1/traces/ingestion/55555555555555555555555555555555')
  })
})
