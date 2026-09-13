// @vitest-environment node

import { afterEach, describe, expect, it, vi } from 'vitest'
// @ts-expect-error Node 22 provides this runtime class; the browser bundle excludes Node ambient types.
import { File as NodeFile } from 'node:buffer'

describe('workspace generated API adapter', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it('streams a browser File as multipart without overriding its boundary', async () => {
    vi.stubGlobal('location', { origin: 'https://example.test' })
    let capturedRequest!: Request
    const fetchMock = vi.fn(async (request: Request) => {
      capturedRequest = request
      return Response.json({
        document_id: '01900000-0000-7000-8000-000000000101',
        version_id: '01900000-0000-7000-8000-000000000102',
        job_id: '01900000-0000-7000-8000-000000000103',
        deduplicated: false,
        status: 'pending',
      }, { status: 202 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { configureApiSession } = await import('../src/api/client')
    const { workspaceApi } = await import('../src/api/workspace')
    configureApiSession({ csrfToken: 'csrf-upload' })
    const file = new NodeFile(['policy'], 'policy.txt', { type: 'text/plain' }) as unknown as File

    await workspaceApi.uploadDocument({
      file,
      collectionId: '01900000-0000-7000-8000-000000000201',
      title: 'Policy',
      visibility: 'tenant',
    })

    const body = await capturedRequest.text()
    expect(capturedRequest.headers.get('X-CSRF-Token')).toBe('csrf-upload')
    expect(capturedRequest.headers.get('Content-Type')).toContain('multipart/form-data; boundary=')
    expect(body).toContain('01900000-0000-7000-8000-000000000201')
    expect(body).toContain('filename="policy.txt"')
    expect(body).toContain('policy')
  })

  it('encodes job status and cursor filters through the generated client', async () => {
    vi.stubGlobal('location', { origin: 'https://example.test' })
    let capturedRequest!: Request
    const fetchMock = vi.fn(async (request: Request) => {
      capturedRequest = request
      return Response.json({ items: [], next_cursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { workspaceApi } = await import('../src/api/workspace')
    await workspaceApi.listJobs({ status: 'failed', cursor: 'cursor-token', limit: 12 })

    expect(capturedRequest.url).toContain('/api/v1/ingestion-jobs?')
    expect(capturedRequest.url).toContain('status=failed')
    expect(capturedRequest.url).toContain('cursor=cursor-token')
    expect(capturedRequest.url).toContain('limit=12')
  })
})
