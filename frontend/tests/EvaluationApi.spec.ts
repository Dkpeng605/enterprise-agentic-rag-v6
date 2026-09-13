// @vitest-environment node

import { afterEach, describe, expect, it, vi } from 'vitest'

describe('evaluation generated API adapter', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it('uses catalog, create, history, detail, and comparison contracts', async () => {
    vi.stubGlobal('location', { origin: 'https://example.test' })
    const requests: Request[] = []
    vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
      requests.push(request)
      if (request.url.endsWith('/catalog')) return Response.json({ dataset_revision: 'golden-v1', dataset_label: 'Golden', case_counts: { all: 30 }, profiles: [], max_cases: 30, max_llm_calls: 0 })
      if (request.method === 'POST') return Response.json({ id: '01900000-0000-7000-8000-000000000001' })
      if (request.url.includes('/compare?')) return Response.json({ comparable: false, reasons: ['MODE_MISMATCH'] })
      if (request.url.includes('?status=succeeded')) return Response.json({ items: [], next_cursor: null })
      return Response.json({ id: '01900000-0000-7000-8000-000000000001' })
    }))
    const { evaluationApi } = await import('../src/api/evaluations')

    await evaluationApi.catalog()
    await evaluationApi.create({ dataset_revision: 'golden-v1', mode: 'all', provider_profile: 'local', max_cases: 5, max_llm_calls: 0 })
    await evaluationApi.list({ status: 'succeeded', limit: 7 })
    await evaluationApi.get('01900000-0000-7000-8000-000000000001')
    await evaluationApi.compare('01900000-0000-7000-8000-000000000001', '01900000-0000-7000-8000-000000000002')

    expect(requests.map((item) => item.url)).toEqual([
      'https://example.test/api/v1/evaluations/catalog',
      'https://example.test/api/v1/evaluations/runs',
      'https://example.test/api/v1/evaluations/runs?status=succeeded&limit=7',
      'https://example.test/api/v1/evaluations/runs/01900000-0000-7000-8000-000000000001',
      'https://example.test/api/v1/evaluations/compare?base_run_id=01900000-0000-7000-8000-000000000001&candidate_run_id=01900000-0000-7000-8000-000000000002',
    ])
    expect(await requests[1]!.json()).toMatchObject({ dataset_revision: 'golden-v1', max_cases: 5 })
  })
})
