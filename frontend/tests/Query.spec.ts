import { describe, expect, it } from 'vitest'

import { parseEventStream } from '../src/api/query'

function streamFrom(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
}

describe('SSE query protocol', () => {
  it('parses ordered frames across arbitrary network chunks', async () => {
    const stream = streamFrom([
      'id: 1\nevent: accepted\ndata: {"query_id":"q-1"}\n',
      '\nid: 2\nevent: progress\ndata: {"stage":"retrieving"}\n\n',
      'id: 3\nevent: completed\ndata: {"query_id":"q-1","status":"answered",',
      '"answer":"有依据的答案","citations":[],"diagnostics":{},"usage":{},"trace_id":"abc"}\n\n',
    ])

    const events = []
    for await (const event of parseEventStream(stream)) events.push(event)

    expect(events.map((event) => event.type)).toEqual(['accepted', 'progress', 'completed'])
    expect(events[1]).toMatchObject({ type: 'progress', stage: 'retrieving' })
    expect(events[2]).toMatchObject({ type: 'completed', result: { answer: '有依据的答案' } })
  })

  it('rejects malformed sequence and payload data instead of guessing', async () => {
    const stream = streamFrom(['id: nope\nevent: accepted\ndata: {}\n\n'])

    await expect(async () => {
      for await (const _event of parseEventStream(stream)) {
        // consume generator
      }
    }).rejects.toThrow('SSE_EVENT_INVALID')
  })

  it('rejects duplicate or decreasing event sequences', async () => {
    const stream = streamFrom([
      'id: 2\nevent: heartbeat\ndata: {}\n\nid: 2\nevent: heartbeat\ndata: {}\n\n',
    ])

    await expect(async () => {
      for await (const _event of parseEventStream(stream)) {
        // consume generator
      }
    }).rejects.toThrow('SSE_EVENT_OUT_OF_ORDER')
  })
})
