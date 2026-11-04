import { flushPromises } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { request } from '../api/client'
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
it('cancels platform status reads on session change without accepting a stale 503 snapshot', async () => {
  const late = deferred<unknown>()
  fakeApi(() => late.promise)
  const pending = getReadiness().catch((error) => error.code)
  clearSession()
  late.resolve(new Response('{}', { status: 503 }))
  expect(await pending).toBe('identity_changed')
})
