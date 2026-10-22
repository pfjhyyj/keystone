import { flushPromises } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { invokeModel, request } from '../api/client'
import { getReadiness } from '../api/system'
import { clearSession, identity } from '../stores/session'
import { authorize, deferred, fakeApi } from './support'

it('aborts an in-flight fetch when the identity changes and discards its abort error', async () => {
  let signal!: AbortSignal
  vi.stubGlobal(
    'fetch',
    vi.fn(
      (_url, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          signal = init.signal as AbortSignal
          signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        }),
    ),
  )
  const pending = request('/users').catch((error) => error.code)
  clearSession()
  expect(signal.aborted).toBe(true)
  expect(await pending).toBe('identity_changed')
})
it('releases completed requests so later invalidation cannot abort their signals', async () => {
  let signal!: AbortSignal
  fakeApi((_path, init) => {
    signal = init.signal as AbortSignal
    return {}
  })
  await request('/users')
  clearSession()
  expect(signal.aborted).toBe(false)
})
it.each([null, 'unauthenticated', '<html>failed</html>'])(
  'preserves HTTP errors with an unexpected body: %j',
  async (body) => {
    authorize(['user:read'])
    fakeApi(
      () =>
        new Response(
          typeof body === 'string' && body.startsWith('<') ? body : JSON.stringify(body),
          { status: 401 },
        ),
    )
    await expect(request('/users')).rejects.toMatchObject({ status: 401 })
    expect(identity.value).toBeNull()
  },
)
it('marks a truncated successful write receipt as unknown and never retries it', async () => {
  fakeApi(() => new Response('{"version":', { status: 200 }))
  await expect(request('/users/id', 'PATCH', {})).rejects.toMatchObject({
    code: 'invalid_response',
    status: 502,
  })
  expect(globalThis.fetch).toHaveBeenCalledOnce()
})
it('supports caller cancellation and serializes false JSON bodies', async () => {
  let body: unknown
  fakeApi((_path, init) => {
    body = init.body
    return false
  })
  expect(await request('/test', 'POST', false)).toBe(false)
  expect(body).toBe('false')
  const gate = new AbortController()
  vi.stubGlobal(
    'fetch',
    vi.fn(
      (_url, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          )
        }),
    ),
  )
  const pending = request('/test', 'GET', undefined, true, gate.signal)
  gate.abort()
  await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
})
it('cancels an idle model response stream immediately on session change', async () => {
  const cancelled = vi.fn(),
    events = vi.fn()
  fakeApi(
    () =>
      new Response(new ReadableStream({ cancel: cancelled }), {
        headers: { 'Content-Type': 'text/event-stream' },
      }),
  )
  const pending = invokeModel({}, events, new AbortController().signal).catch((error) => error.code)
  await flushPromises()
  clearSession()
  expect(await pending).toBe('identity_changed')
  expect(cancelled).toHaveBeenCalledOnce()
  expect(events).not.toHaveBeenCalled()
})
it('preserves model deltas, completed streams and non-streaming responses', async () => {
  const events = vi.fn()
  const bytes = new TextEncoder().encode(
    'data: {"type":"delta","text":"你好"}\n\ndata: {"type":"done"}\n\n',
  )
  fakeApi(
    () =>
      new Response(
        new ReadableStream({
          start(controller) {
            for (let i = 0; i < bytes.length; i += 3) controller.enqueue(bytes.slice(i, i + 3))
            controller.close()
          },
        }),
        { headers: { 'Content-Type': 'text/event-stream' } },
      ),
  )
  await invokeModel({}, events, new AbortController().signal)
  expect(events.mock.calls.map(([event]) => event)).toEqual([
    { type: 'delta', text: '你好' },
    { type: 'done' },
  ])
  fakeApi(() => ({ type: 'done', content: 'json result' }))
  await invokeModel({}, events, new AbortController().signal)
  expect(events).toHaveBeenLastCalledWith({ type: 'done', content: 'json result' })
})
it('rejects incomplete model streams without retry and discards late non-stream output', async () => {
  fakeApi(
    () =>
      new Response('data: {"type":"delta","text":"partial"}\n\n', {
        headers: { 'Content-Type': 'text/event-stream' },
      }),
  )
  await expect(invokeModel({}, vi.fn(), new AbortController().signal)).rejects.toThrow(
    '响应未完整结束',
  )
  expect(globalThis.fetch).toHaveBeenCalledOnce()
  const late = deferred<unknown>(),
    event = vi.fn()
  fakeApi(() => late.promise)
  const pending = invokeModel({}, event, new AbortController().signal).catch((error) => error.code)
  clearSession()
  late.resolve({ type: 'done', content: 'private output' })
  expect(await pending).toBe('identity_changed')
  expect(event).not.toHaveBeenCalled()
})

it('cancels platform status reads on session change without accepting a stale 503 snapshot', async () => {
  const late = deferred<unknown>()
  fakeApi(() => late.promise)
  const pending = getReadiness().catch((error) => error.code)
  clearSession()
  late.resolve(new Response('{}', { status: 503 }))
  expect(await pending).toBe('identity_changed')
})

it('stops delivering buffered model events when a caller cancels during a delta', async () => {
  const controller = new AbortController(),
    event = vi.fn(() => controller.abort())
  fakeApi(
    () =>
      new Response('data: {"type":"delta","text":"first"}\n\ndata: {"type":"done"}\n\n', {
        headers: { 'Content-Type': 'text/event-stream' },
      }),
  )
  await expect(invokeModel({}, event, controller.signal)).rejects.toMatchObject({
    name: 'AbortError',
  })
  expect(event).toHaveBeenCalledOnce()
})
