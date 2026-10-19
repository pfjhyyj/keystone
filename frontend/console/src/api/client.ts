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

// Per-tab memory only. The session secret is carried exclusively by the HttpOnly Cookie.
let csrf = ''
export function setCsrf(value: string | null) {
  csrf = value ?? ''
}
let epoch = 0
let errorObserver: (error: ApiError) => void = () => {}
export function invalidateRequests() {
  epoch++
}
export function observeErrors(observer: (error: ApiError) => void) {
  errorObserver = observer
}
function current(started: number) {
  if (started !== epoch) throw new ApiError(409, 'identity_changed', '登录状态已变化，请重新操作。')
}
async function check(response: Response, started: number, observe = true) {
  current(started)
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    if (started !== epoch)
      throw new ApiError(409, 'identity_changed', '登录状态已变化，请重新操作。')
    const error = new ApiError(
      response.status,
      data.code ?? 'unavailable',
      data.detail ?? '服务暂时无法访问',
    )
    if (observe) errorObserver(error)
    throw error
  }
}

export async function invokeModel(
  body: unknown,
  onEvent: (event: Record<string, unknown>) => void,
  signal: AbortSignal,
) {
  const started = epoch
  const response = await fetch('/api/v1/models/invoke', {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    signal: AbortSignal.any([signal, AbortSignal.timeout(65000)]),
    headers: {
      'Content-Type': 'application/json',
      'X-Keystone-Request': '1',
      'X-CSRF-Token': csrf,
    },
    body: JSON.stringify(body),
  })
  await check(response, started)
  if (!response.headers.get('content-type')?.includes('text/event-stream')) {
    const value = await response.json()
    current(started)
    onEvent(value)
    return
  }
  const reader = response.body?.getReader()
  if (!reader) throw new Error('没有收到响应流')
  const decoder = new TextDecoder()
  let buffer = '',
    completed = false
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      buffer += decoder.decode(chunk.value, { stream: true })
      let end: number
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, end)
        buffer = buffer.slice(end + 2)
        if (block.startsWith('data: ')) {
          const event = JSON.parse(block.slice(6))
          current(started)
          if (event.type === 'error') {
            const error = new ApiError(
              typeof event.http_status === 'number' ? event.http_status : 502,
              String(event.code ?? 'model_response_unknown'),
              String(event.detail ?? '模型响应失败'),
            )
            errorObserver(error)
            throw error
          }
          onEvent(event)
          if (event.type === 'done') completed = true
        }
      }
    }
    if (!completed) throw new Error('响应未完整结束，调用结果可能未知，请查看记录；不会自动重试。')
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

export async function uploadFile(file: File, requestKey: string, storageKey?: string) {
  const started = epoch
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
  const checksum = Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, '0'),
  ).join('')
  current(started)
  const params = new URLSearchParams({
    name: file.name,
    expected_size: String(file.size),
    content_type: file.type || 'application/octet-stream',
    request_key: requestKey,
    checksum_sha256: checksum,
  })
  if (storageKey) params.set('storage_key', storageKey)
  const response = await fetch(`/api/v1/files?${params}`, {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    signal: AbortSignal.timeout(65000),
    headers: {
      'Content-Type': 'application/octet-stream',
      'X-Keystone-Request': '1',
      'X-CSRF-Token': csrf,
    },
    body: file,
  })
  await check(response, started)
  const result = await response.json()
  current(started)
  return result
}

export async function downloadFile(id: string, name: string) {
  const started = epoch
  const response = await fetch(`/api/v1/files/${id}/content`, {
    credentials: 'same-origin',
    cache: 'no-store',
  })
  await check(response, started)
  const blob = await response.blob()
  await check(response, started)
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function request<T>(
  path: string,
  method = 'GET',
  body?: unknown,
  observe = true,
): Promise<T> {
  const started = epoch
  const response = await fetch(`/api/v1${path}`, {
    method,
    credentials: 'same-origin',
    cache: 'no-store',
    signal: AbortSignal.timeout(15000),
    headers: {
      'X-Keystone-Request': '1',
      ...(body ? { 'Content-Type': 'application/json' } : {}),
      ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  })
  await check(response, started, observe)
  const result = response.status === 204 ? undefined : await response.json()
  await check(response, started, observe)
  return result as T
}
