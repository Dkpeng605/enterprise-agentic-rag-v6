import { apiClient, apiErrorFromResponse } from './client'
import type { components } from './schema'

export type QueryInput = components['schemas']['QueryRequestModel']
export type QueryResult = components['schemas']['QueryResponseModel']
export type Citation = components['schemas']['QueryCitationResponse']
export type Collection = components['schemas']['CollectionResponse']

export type QueryStreamEvent =
  | { type: 'accepted'; sequence: number; queryId: string }
  | { type: 'progress'; sequence: number; stage: string; detail?: string }
  | { type: 'heartbeat'; sequence: number }
  | { type: 'completed'; sequence: number; result: QueryResult }
  | { type: 'error'; sequence: number; queryId?: string; code: string; message: string }

export interface QueryApi {
  listCollections(): Promise<Collection[]>
  stream(input: QueryInput, signal: AbortSignal, onEvent: (event: QueryStreamEvent) => void): Promise<void>
}

type SseFrame = { id?: string; event?: string; data: string }

function decodeFrame(raw: string): SseFrame | null {
  const frame: SseFrame = { data: '' }
  const data: string[] = []
  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue
    const separator = line.indexOf(':')
    const field = separator === -1 ? line : line.slice(0, separator)
    const value = separator === -1 ? '' : line.slice(separator + 1).replace(/^ /, '')
    if (field === 'id') frame.id = value
    if (field === 'event') frame.event = value
    if (field === 'data') data.push(value)
  }
  frame.data = data.join('\n')
  return frame.event || frame.data ? frame : null
}

function parseEvent(frame: SseFrame): QueryStreamEvent {
  const sequence = Number(frame.id)
  if (!Number.isSafeInteger(sequence) || sequence <= 0) throw new Error('SSE_EVENT_INVALID')
  let payload: Record<string, unknown>
  try {
    payload = frame.data ? (JSON.parse(frame.data) as Record<string, unknown>) : {}
  } catch {
    throw new Error('SSE_EVENT_INVALID')
  }
  if (frame.event === 'accepted' && typeof payload.query_id === 'string') {
    return { type: 'accepted', sequence, queryId: payload.query_id }
  }
  if (frame.event === 'progress' && typeof payload.stage === 'string') {
    return {
      type: 'progress',
      sequence,
      stage: payload.stage,
      detail: typeof payload.detail === 'string' ? payload.detail : undefined,
    }
  }
  if (frame.event === 'heartbeat') return { type: 'heartbeat', sequence }
  if (frame.event === 'completed' && isQueryResult(payload)) {
    return { type: 'completed', sequence, result: payload }
  }
  if (frame.event === 'error' && typeof payload.code === 'string') {
    return {
      type: 'error',
      sequence,
      queryId: typeof payload.query_id === 'string' ? payload.query_id : undefined,
      code: payload.code,
      message: typeof payload.message === 'string' ? payload.message : '查询执行失败。',
    }
  }
  throw new Error('SSE_EVENT_INVALID')
}

function isQueryResult(value: Record<string, unknown>): value is QueryResult {
  return (
    typeof value.query_id === 'string' &&
    ['answered', 'abstained', 'no_results'].includes(String(value.status)) &&
    typeof value.answer === 'string' &&
    Array.isArray(value.citations) &&
    value.citations.every(isCitation) &&
    value.diagnostics !== null &&
    typeof value.diagnostics === 'object' &&
    value.usage !== null &&
    typeof value.usage === 'object'
  )
}

function isCitation(value: unknown): value is Citation {
  if (value === null || typeof value !== 'object') return false
  const citation = value as Record<string, unknown>
  return (
    typeof citation.id === 'number' &&
    typeof citation.document_id === 'string' &&
    typeof citation.root_id === 'string' &&
    Array.isArray(citation.chunk_ids) &&
    citation.chunk_ids.every((item) => typeof item === 'string') &&
    typeof citation.source_name === 'string' &&
    typeof citation.title === 'string' &&
    typeof citation.quote === 'string'
  )
}

export async function* parseEventStream(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<QueryStreamEvent> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let lastSequence = 0
  const ordered = (frame: SseFrame): QueryStreamEvent => {
    const event = parseEvent(frame)
    if (event.sequence <= lastSequence) throw new Error('SSE_EVENT_OUT_OF_ORDER')
    lastSequence = event.sequence
    return event
  }
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const raw = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const frame = decodeFrame(raw)
        if (frame) yield ordered(frame)
        boundary = buffer.indexOf('\n\n')
      }
      if (done) break
    }
    if (buffer.trim()) {
      const frame = decodeFrame(buffer)
      if (frame) yield ordered(frame)
    }
  } finally {
    reader.releaseLock()
  }
}

async function parseErrorBody(stream: ReadableStream<Uint8Array> | null): Promise<unknown> {
  if (!stream) return undefined
  const text = await new Response(stream).text()
  try {
    return JSON.parse(text) as unknown
  } catch {
    return undefined
  }
}

export const queryApi: QueryApi = {
  async listCollections() {
    const result = await apiClient.GET('/api/v1/collections')
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data.items
  },
  async stream(input, signal, onEvent) {
    const result = await apiClient.POST('/api/v1/queries/stream', {
      body: input,
      parseAs: 'stream',
      signal,
    })
    const body = (result.data ?? result.error) as ReadableStream<Uint8Array> | undefined
    if (!result.response.ok) {
      throw apiErrorFromResponse(await parseErrorBody(body ?? null), result.response)
    }
    if (!body) throw new Error('SSE_STREAM_MISSING')
    for await (const event of parseEventStream(body)) onEvent(event)
  },
}
