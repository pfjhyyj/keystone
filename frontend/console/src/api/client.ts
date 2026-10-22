export interface User {
  id: string
  username: string
  display_name: string
  password_change_required: boolean
}
export interface Identity {
  user: User
  permissions: string[]
  is_administrator: boolean
  credential_kind: 'session' | 'pat'
  csrf_token: string | null
}
export interface PersonalToken {
  id: string
  name: string
  permissions: string[]
  expires_at: string
  revoked_at: string | null
  token?: string
}
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message)
  }
}
// The session secret stays in the HttpOnly Cookie; only CSRF is kept in memory.
let csrf = ''
let epoch = 0
let errorObserver: (error: ApiError) => void = () => {}
const controllers = new Set<AbortController>()
export const currentEpoch = () => epoch
export function setCsrf(value: string | null) {
  csrf = value ?? ''
}
export function invalidateRequests() {
  epoch++
  controllers.forEach((controller) => controller.abort())
}
export function observeErrors(observer: typeof errorObserver) {
  errorObserver = observer
}
export const headers = () => ({
  'X-Keystone-Request': '1',
  ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
})
export function failure(status: number, data: unknown) {
  const body = (data && typeof data === 'object' ? data : {}) as {
    code?: unknown
    detail?: unknown
  }
  return new ApiError(
    status,
    typeof body.code === 'string' && body.code ? body.code : 'unavailable',
    typeof body.detail === 'string' ? body.detail : '服务暂时无法访问',
  )
}
export function sessionWork(signal?: AbortSignal, timeout?: number) {
  const started = epoch,
    controller = new AbortController()
  controllers.add(controller)
  let reported: unknown
  const assert = () => {
    if (started !== epoch)
      throw new ApiError(409, 'identity_changed', '登录状态已变化，请重新操作。')
  }
  return {
    signal: AbortSignal.any([
      controller.signal,
      ...(signal ? [signal] : []),
      ...(timeout === undefined ? [] : [AbortSignal.timeout(timeout)]),
    ]),
    assert,
    fail(error: ApiError, observe = true): never {
      assert()
      // An observer may invalidate this request. Preserve its original error.
      reported = error
      if (observe) errorObserver(error)
      throw error
    },
    rethrow(error: unknown): never {
      if (reported === undefined || error !== reported) assert()
      throw error
    },
    close() {
      controllers.delete(controller)
    },
  }
}
async function check(response: Response, work: ReturnType<typeof sessionWork>, observe = true) {
  work.assert()
  if (!response.ok) {
    const data = await response.json().catch(() => undefined)
    work.fail(failure(response.status, data), observe)
  }
}
export async function invokeModel(
  body: unknown,
  onEvent: (event: Record<string, unknown>) => void,
  signal: AbortSignal,
) {
  const work = sessionWork(signal, 65000)
  try {
    const response = await fetch('/api/v1/models/invoke', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      signal: work.signal,
      headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    await check(response, work)
    if (!response.headers.get('content-type')?.includes('text/event-stream')) {
      const value = await response.json()
      work.assert()
      work.signal.throwIfAborted()
      onEvent(value)
      return
    }
    const reader = response.body?.getReader()
    if (!reader) throw new Error('没有收到响应流')
    const cancel = () => {
      void reader.cancel().catch(() => {})
    }
    work.signal.addEventListener('abort', cancel, { once: true })
    if (work.signal.aborted) cancel()
    const decoder = new TextDecoder()
    let buffer = '',
      completed = false
    try {
      while (true) {
        const chunk = await reader.read()
        work.assert()
        work.signal.throwIfAborted()
        if (chunk.done) break
        buffer += decoder.decode(chunk.value, { stream: true })
        let end: number
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0, end)
          buffer = buffer.slice(end + 2)
          if (block.startsWith('data: ')) {
            const event = JSON.parse(block.slice(6))
            work.assert()
            work.signal.throwIfAborted()
            if (event.type === 'error')
              work.fail(
                new ApiError(
                  typeof event.http_status === 'number' ? event.http_status : 502,
                  String(event.code ?? 'model_response_unknown'),
                  String(event.detail ?? '模型响应失败'),
                ),
              )
            onEvent(event)
            if (event.type === 'done') completed = true
          }
        }
      }
      if (!completed)
        throw new Error('响应未完整结束，调用结果可能未知，请查看记录；不会自动重试。')
    } finally {
      work.signal.removeEventListener('abort', cancel)
      await reader.cancel().catch(() => {})
      reader.releaseLock()
    }
  } catch (error) {
    return work.rethrow(error)
  } finally {
    work.close()
  }
}
export async function request<T>(
  path: string,
  method = 'GET',
  body?: unknown,
  observe = true,
  signal?: AbortSignal,
  timeout = 15000,
): Promise<T> {
  const work = sessionWork(signal, timeout)
  try {
    const response = await fetch(`/api/v1${path}`, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: work.signal,
      headers: {
        ...headers(),
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    await check(response, work, observe)
    const result =
      response.status === 204
        ? undefined
        : await response.json().catch(() => {
            throw new ApiError(
              502,
              'invalid_response',
              '响应不完整，请先确认操作结果，再用原操作重试。',
            )
          })
    work.assert()
    work.signal.throwIfAborted()
    return result as T
  } catch (error) {
    return work.rethrow(error)
  } finally {
    work.close()
  }
}
