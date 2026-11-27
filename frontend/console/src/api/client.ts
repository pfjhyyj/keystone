export interface User {
  id: string
  username: string
  display_name: string
  password_change_required: boolean
}
export interface Identity {
  user: User
  permissions: string[]
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
  nextSessionCheck = 0
  controllers.forEach((controller) => controller.abort())
}
export function observeErrors(observer: typeof errorObserver) {
  errorObserver = observer
}
export const headers = (path: string) => ({
  'X-Keystone-Request': '1',
  ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
  ...sessionActivityHeaders(path),
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
let lastInteraction = -Infinity
let nextSessionCheck = 0
let lastSessionCheckWasActive = false
for (const event of ['pointerdown', 'keydown', 'wheel']) {
  globalThis.window?.addEventListener(event, markSessionActive, {
    capture: true,
    passive: true,
  })
}

function markSessionActive() {
  lastInteraction = Date.now()
}

function sessionActivityHeaders(path: string): Record<string, string> {
  const now = Date.now()
  const active = now - lastInteraction < 60_000
  if (
    !csrf ||
    (now < nextSessionCheck && (lastSessionCheckWasActive || !active)) ||
    (path.startsWith('/auth/') && path !== '/auth/me')
  )
    return {}
  nextSessionCheck = now + 5 * 60 * 1000
  lastSessionCheckWasActive = active
  return { 'X-Keystone-Session-Active': active ? '1' : '0' }
}

interface RequestOptions {
  method?: string
  body?: unknown
  observe?: boolean
  signal?: AbortSignal
  timeout?: number
  extraHeaders?: Record<string, string>
}

async function fetchJson<T>(
  path: string,
  decode: (data: unknown, response: Response) => T,
  {
    method = 'GET',
    body,
    observe = true,
    signal,
    timeout = 15000,
    extraHeaders,
  }: RequestOptions = {},
): Promise<T> {
  const work = sessionWork(signal, timeout)
  try {
    const response = await fetch(`/api/v1${path}`, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: work.signal,
      headers: {
        ...headers(path),
        ...extraHeaders,
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
    return decode(result, response)
  } catch (error) {
    return work.rethrow(error)
  } finally {
    work.close()
  }
}

export function request<T>(
  path: string,
  method = 'GET',
  body?: unknown,
  observe = true,
  signal?: AbortSignal,
  timeout = 15000,
): Promise<T> {
  return fetchJson(path, (data) => data as T, { method, body, observe, signal, timeout })
}

export interface PageResult<T> {
  items: T[]
  total: number | null
}

export function requestPage<T>(
  path: string,
  offset: number,
  limit: number,
): Promise<PageResult<T>> {
  const separator = path.indexOf('?')
  const base = separator === -1 ? path : path.slice(0, separator)
  const filters = new URLSearchParams(separator === -1 ? '' : path.slice(separator + 1))
  filters.delete('offset')
  filters.delete('limit')
  const params = new URLSearchParams([
    ['offset', String(offset)],
    ['limit', String(limit)],
    ...filters,
  ])
  return fetchJson(
    `${base}?${params}`,
    (data, response) => {
      if (!Array.isArray(data)) throw new ApiError(502, 'invalid_response', '列表响应格式不正确。')
      const raw = response.headers.get('X-Total-Count')
      const total = raw !== null && /^[0-9]+$/.test(raw) ? Number(raw) : NaN
      return { items: data as T[], total: Number.isSafeInteger(total) ? total : null }
    },
    { extraHeaders: { 'X-Keystone-List-Total': '1' } },
  )
}

export const uuidPattern =
  '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
